"""Versioned research presets: durable SQLite snapshots, never trading permission."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from orb.ingestion import _object, _invalid_constant
from orb.research_settings import ResearchSettings, settings_contract

REFERENCE = 'orb-reference'
APP_ID = 0x4F524250


class PresetConflict(ValueError):
    pass


def _name(name):
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            or not name.isprintable()):
        raise ValueError('preset name must be 1–80 printable characters')
    return name.strip()


class PresetStore:
    """One local DB. Immutable versions; revision checks prevent stale edits.

    BEGIN IMMEDIATE serialises competing writers; FULL rollback journalling is
    used for file-backed stores. This is neither a broker ledger nor a lease.
    """
    def __init__(self, path=':memory:'):
        path = str(path)
        if path != ':memory:':
            p = Path(path)
            if p.is_symlink():
                raise ValueError('preset database symlinks are not supported')
            p.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA synchronous=FULL')
            with self._transaction():
                identity = self.db.execute('PRAGMA application_id').fetchone()[0]
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                if identity == version == 0 and not tables:
                    self.db.execute('CREATE TABLE preset_versions (preset_id TEXT NOT NULL, '
                        'version INTEGER NOT NULL CHECK(version > 0), name TEXT NOT NULL, '
                        'settings TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, '
                        'PRIMARY KEY(preset_id, version))')
                    self.db.execute(f'PRAGMA application_id={APP_ID}')
                    self.db.execute('PRAGMA user_version=1')
                elif identity != APP_ID or version != 1 or tables != ['preset_versions']:
                    raise ValueError('unrecognised preset database; no migration attempted')
            self.db.execute('PRAGMA journal_mode=DELETE')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    @staticmethod
    def reference():
        s = ResearchSettings()
        return dict(id=REFERENCE, version=1, name='ORB Reference', settings=s.to_dict(),
                    settings_sha256=s.fingerprint, readonly=True, created_at=None)

    def _record(self, row):
        if row is None:
            raise KeyError('preset version not found')
        s = ResearchSettings.from_dict(json.loads(row['settings'], object_pairs_hook=_object))
        if s.fingerprint != row['fingerprint']:
            raise ValueError('preset settings integrity mismatch')
        return dict(id=row['preset_id'], version=row['version'], name=row['name'],
                    settings=s.to_dict(), settings_sha256=s.fingerprint,
                    readonly=False, created_at=row['created_at'])

    def get(self, preset_id, version=None):
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError('preset version must be a positive integer')
        if preset_id == REFERENCE:
            if version not in (None, 1):
                raise KeyError('reference version not found')
            return self.reference()
        with self.lock:
            if version is None:
                row = self.db.execute('SELECT * FROM preset_versions WHERE preset_id=? '
                                      'ORDER BY version DESC LIMIT 1', (preset_id,)).fetchone()
            else:
                row = self.db.execute('SELECT * FROM preset_versions WHERE preset_id=? AND version=?',
                                      (preset_id, version)).fetchone()
            return self._record(row)

    def list(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute('SELECT DISTINCT preset_id FROM preset_versions ORDER BY preset_id')]
            return [self.reference(), *(self.get(p) for p in ids)]

    def history(self, preset_id):
        if preset_id == REFERENCE:
            return [self.reference()]
        with self.lock:
            rows = list(self.db.execute('SELECT * FROM preset_versions WHERE preset_id=? ORDER BY version', (preset_id,)))
            if not rows:
                raise KeyError('preset not found')
            return [self._record(r) for r in rows]

    def save(self, name, settings, *, preset_id=None, expected_version=None):
        name = _name(name)
        s = ResearchSettings.from_dict(settings)
        if preset_id == REFERENCE:
            raise PresetConflict('ORB Reference is fixed; duplicate it to create a variant')
        if preset_id is None and expected_version is not None:
            raise ValueError('new preset must not specify an expected version')
        with self._transaction():
            if preset_id is None:
                if self.db.execute('SELECT COUNT(DISTINCT preset_id) FROM preset_versions').fetchone()[0] >= 100:
                    raise PresetConflict('local preset capacity reached')
                preset_id, version = uuid4().hex, 1
            else:
                current = self.get(preset_id)
                if type(expected_version) is not int or expected_version != current['version']:
                    raise PresetConflict('preset changed; reload before saving')
                if expected_version >= 1000:
                    raise PresetConflict('local version capacity reached')
                version = expected_version + 1
            self.db.execute('INSERT INTO preset_versions VALUES(?,?,?,?,?,?)',
                (preset_id, version, name, json.dumps(s.to_dict(), sort_keys=True),
                 s.fingerprint, datetime.now(timezone.utc).isoformat()))
            return self.get(preset_id, version)

    def duplicate(self, preset_id, version, name):
        if type(version) is not int or version < 1:
            raise ValueError('duplicate requires an exact saved version')
        source = self.get(preset_id, version)
        return self.save(name, source['settings'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('results/dashboard-presets.sqlite'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('template')
    commands.add_parser('list')
    for action in ('create', 'save'):
        p = commands.add_parser(action)
        p.add_argument('--name', required=True)
        p.add_argument('--settings', type=Path, required=True)
        if action == 'save':
            p.add_argument('--id', required=True)
            p.add_argument('--expected-version', type=int, required=True)
    p = commands.add_parser('run', help='reevaluate a CSV with an exact saved risk/cost version')
    p.add_argument('--id', required=True)
    p.add_argument('--version', type=int, required=True)
    p.add_argument('--csv', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'template':
            result = settings_contract()
        else:
            with PresetStore(args.db) as store:
                if args.command == 'list':
                    result = store.list()
                elif args.command in ('create', 'save'):
                    if args.settings.stat().st_size > 8192:
                        raise ValueError('settings file exceeds 8 KiB')
                    settings = json.loads(args.settings.read_text(), object_pairs_hook=_object,
                                          parse_constant=_invalid_constant)
                    result = store.save(args.name, settings, **(dict(preset_id=args.id,
                        expected_version=args.expected_version) if args.command == 'save' else {}))
                else:
                    from orb.dashboard_import import import_dashboard, MAX_BYTES
                    from orb.profile_runs import apply_preset
                    if args.csv.stat().st_size > MAX_BYTES:
                        raise ValueError('source exceeds 10 MiB')
                    original = import_dashboard(dict(kind='csv', source=args.csv.read_text(encoding='utf-8-sig')))
                    result = apply_preset(original, store.get(args.id, args.version))
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    # Exclusive write also protects the CSV and preset database.
                    with args.output.open('x', encoding='utf-8') as output:
                        json.dump(result, output, indent=2)
                        output.write('\n')
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'Preset error: {exc}\n')


if __name__ == '__main__':
    main()
