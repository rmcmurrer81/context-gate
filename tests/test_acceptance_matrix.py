from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.acceptance_matrix import main

ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_matrix_passes_and_reports_key_real_world_checks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0

    output = capsys.readouterr().out
    assert "PASS Pattern Lab summary: 3 ALLOW / 3 REVIEW / 3 BLOCK" in output
    assert "35 Main St=3; 76 New Avenue Suite 232=8" in output
    assert "Suite 354=REVIEW with 8-source trace" in output
    assert "35 + '78 more' = 113" in output
    assert "replaces 35 with 78" in output
    assert "different event=REVIEW" in output
    assert "no number=REVIEW; no total fabricated" in output
    assert "original evidence preserved" in output
    assert "explicit B1 guidance retrieved, then append-only retracted" in output
    assert "advisory BLOCK->ALLOW resolved with original receipt preserved" in output
    assert "retraction restored no active correction" in output
    assert "PASS acceptance matrix complete: 17/17 checks" in output
    assert "FAIL" not in output


def test_acceptance_matrix_is_directly_runnable() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "acceptance_matrix.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS acceptance matrix complete: 17/17 checks" in completed.stdout
    assert completed.stderr == ""
