"""Generate reproducible, explicitly synthetic dashboard acceptance examples."""

import argparse
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json
from pathlib import Path

from orb.dashboard import build_dashboard_run
from orb.domain import Bar
from orb.evaluation import render_report
from orb.session import exchange_session


def scenarios():
    session = exchange_session(date(2026, 10, 9))
    bars = [Bar(symbol, session.opens_at + timedelta(minutes=i),
                D('99'), D('100'), D('98'), D('99.5'), 10)
            for i in range(390) for symbol in ('SPY', 'QQQ')]
    skipped = list(bars)
    for i, b in enumerate(skipped):
        offset = int((b.start - session.opens_at).total_seconds() // 60)
        if b.symbol == 'SPY' and offset == 15:
            skipped[i] = replace(b, high=D('101'), close=D('100.2'))
        if b.symbol == 'SPY' and offset == 16:
            skipped[i] = replace(b, open=D('98'))
    complete = list(skipped)
    for i, b in enumerate(complete):
        if b.symbol == 'SPY' and b.start == session.opens_at + timedelta(minutes=16):
            complete[i] = replace(b, open=D('100.3'), high=D('104'), low=D('100'), close=D('103'))
    incomplete = [b for b in complete if not (b.symbol == 'SPY' and
                   b.start == session.opens_at + timedelta(minutes=16))]
    return {'complete_trade': complete, 'known_skip': skipped, 'missing_entry': incomplete}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    # Exclusive destination prevents accidental replacement of source data.
    args.out_dir.mkdir(parents=True, exist_ok=False)
    for name, bars in scenarios().items():
        run = build_dashboard_run(bars, data_label='synthetic-dashboard-' + name)
        (args.out_dir / (name + '.json')).write_text(json.dumps(run, indent=2) + '\n', encoding='utf-8')
        (args.out_dir / (name + '.md')).write_text(render_report(run['evidence']), encoding='utf-8')
    print('Wrote three synthetic dashboard runs and reports; no provider or orders.')


if __name__ == '__main__':
    main()
