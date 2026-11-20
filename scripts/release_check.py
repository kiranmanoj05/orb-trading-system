"""Run the existing local acceptance suites and retain exact, bounded evidence.

This is engineering verification with injected data, never provider acceptance.
Run from any directory with the project's installed development Python.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
BROWSERS = (
    ("workspace", "workspace-browser.mjs", ()),
    ("presets", "presets-browser.mjs", ()),
    ("research", "research-browser.mjs", ()),
    ("comparisons", "comparison-browser.mjs", ()),
    ("paper", "paper-workspace.mjs", ()),
    ("performance", "performance-browser.mjs", ()),
    ("alignment", "alignment-browser.mjs", ()),
    ("retirement", "retirement-browser.mjs", ()),
    ("management", "retirement-browser.mjs", ("--management",)),
    ("status-failure", "retirement-browser.mjs", ("--management", "--status-failure")),
)


def source_identity(root: Path) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args])
    names = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""})
    digest = hashlib.sha256()
    for name in names:
        path = root / os.fsdecode(name)
        digest.update(name + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"missing")
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "head_tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "working_source_sha256": digest.hexdigest(),
            "dirty": bool(git("status", "--porcelain")), "file_count": len(names)}


def run_step(name: str, command: list[str], root: Path, output: Path,
             env: dict[str, str], timeout: float) -> dict:
    started = time.monotonic()
    log = output / (name + ".log")
    result = {"name": name, "command": command, "log": str(log)}
    with log.open("wb") as stream:
        try:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            stream.write(str(error).encode())
            result.update(status="failed", reason="could_not_start", returncode=None)
        else:
            try:
                code = process.wait(timeout=timeout)
                result.update(status="passed" if code == 0 else "failed", returncode=code)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                result.update(status="failed", reason="timeout", returncode=process.returncode)
    result.update(seconds=round(time.monotonic() - started, 3),
                  log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new evidence directory outside the checkout")
    parser.add_argument("--browser", type=Path, required=True,
                        help="existing Chromium/headless-shell executable; never downloaded")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per command")
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents or args.timeout <= 0:
        parser.error("output must be outside the checkout and timeout must be positive")
    if not args.browser.is_file() or not os.access(args.browser, os.X_OK):
        parser.error("browser must be an existing executable")
    output.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APCA_", "ALPACA_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    # Keep the venv executable path: resolving its symlink would select base Python.
    env.update(ORB_PYTHON=sys.executable, BROWSER_EXECUTABLE_PATH=str(args.browser.resolve()))
    steps = [
        ("python", [sys.executable, "-m", "pytest", "-q"]),
        ("dependencies", [sys.executable, "-m", "pip", "check"]),
        ("frontend-install", ["npm", "--prefix", "web", "ci"]),
        ("frontend-build", ["npm", "--prefix", "web", "run", "build"]),
        ("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts"]),
        ("dashboard-cli", [sys.executable, "-m", "orb.dashboard_api", "--help"]),
        ("study-cli", [sys.executable, "-m", "orb.paper_alignment", "--help"]),
        ("whitespace", ["git", "diff", "--check"]),
        *[(name, ["node", "web/tests/" + file, *options]) for name, file, options in BROWSERS],
    ]
    report = {"schema_version": "orb-local-release-check-v1",
              "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "local engineering; injected data; no provider/Mac/CI/profitability claim",
              "source_before": source_identity(ROOT), "python": sys.version,
              "python_executable": sys.executable, "browser": str(args.browser.resolve()),
              "steps": [], "complete": False}
    target = output / "acceptance.json"
    def save() -> None:
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(target)
    save()
    for name, command in steps:
        result = run_step(name, command, ROOT, output, env, args.timeout)
        report["steps"].append(result)
        save()
        print(name + ": " + result["status"], flush=True)
        if result["status"] != "passed":
            report["not_run"] = [n for n, _ in steps[len(report["steps"]):]]
            save()
            return 1
    report["source_after"] = source_identity(ROOT)
    report["complete"] = report["source_before"] == report["source_after"]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not report["complete"]:
        report["failure"] = "source_changed_during_verification"
    save()
    print(str(target), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
