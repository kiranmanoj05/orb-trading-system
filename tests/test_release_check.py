"""Acceptance evidence must retain failures and reject changing source."""
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


CHECK = runpy.run_path(str(Path(__file__).parents[1] / "scripts/release_check.py"))


def test_failed_command_keeps_real_exit_and_log(tmp_path):
    result = CHECK["run_step"]("failure", [sys.executable, "-c", "print('broken'); raise SystemExit(7)"],
                               tmp_path, tmp_path, os.environ.copy(), 10)
    assert result["status"] == "failed" and result["returncode"] == 7
    assert Path(result["log"]).read_text() == "broken\n"
    assert result["log_sha256"] == hashlib.sha256(b"broken\n").hexdigest()


def test_missing_executable_is_failed_not_passed(tmp_path):
    result = CHECK["run_step"]("missing", [str(tmp_path / "missing")],
                               tmp_path, tmp_path, os.environ.copy(), 10)
    assert result["status"] == "failed" and result["reason"] == "could_not_start"
    assert result["returncode"] is None


def test_timeout_is_failed_and_process_reaped(tmp_path):
    result = CHECK["run_step"]("timeout", [sys.executable, "-c", "import time; time.sleep(30)"],
                               tmp_path, tmp_path, os.environ.copy(), .1)
    assert result["status"] == "failed" and result["reason"] == "timeout"
    assert result["returncode"] < 0


def test_source_identity_includes_uncommitted_new_and_deleted_files(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init")
    file = tmp_path / "source.py"
    file.write_text("original\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    identity = CHECK["source_identity"]
    original = identity(tmp_path)
    assert not original["dirty"]
    file.write_text("changed\n")
    changed = identity(tmp_path)
    assert changed["head_tree"] == original["head_tree"] and changed["dirty"]
    assert changed["working_source_sha256"] != original["working_source_sha256"]
    new = tmp_path / "new.py"
    new.write_text("new\n")
    assert identity(tmp_path)["working_source_sha256"] != changed["working_source_sha256"]
    file.unlink()
    assert identity(tmp_path)["file_count"] == 2


@pytest.mark.parametrize("argument", ["--output", "--timeout"])
def test_bad_inputs_cannot_create_acceptance_report(tmp_path, monkeypatch, argument):
    monkeypatch.setattr(sys, "argv", ["release_check", "--output", str(tmp_path / "evidence"),
                                     "--browser", sys.executable,
                                     *(["--output", str(CHECK["ROOT"] / "results")] if argument == "--output"
                                       else ["--timeout", "0"])])
    with pytest.raises(SystemExit) as error:
        CHECK["main"]()
    assert error.value.code == 2
    assert not (tmp_path / "evidence").exists()
