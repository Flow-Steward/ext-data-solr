"""The real Core validator executes all bundled example workflows."""

import os
from pathlib import Path
import subprocess


def test_core_workflow_import_and_runtime_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["/usr/local/bin/python", str(root / "scripts/validate_workflows.py"), str(root)],
        env={**os.environ, "PYTHONPATH": "/app"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
