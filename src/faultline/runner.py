"""Targeted pytest execution with bounded subprocess handling."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .models import TestExecutionResult


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate a process and its group without shell interpolation."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.kill()


def run_targeted_test(
    project_root: str | Path,
    test_node_id: str,
    *,
    timeout: float = 5.0,
    python_executable: str | Path | None = None,
) -> TestExecutionResult:
    """Run one pytest node id and capture all observable execution evidence."""
    root = Path(project_root).resolve()
    executable = str(python_executable or sys.executable)
    command = [executable, "-m", "pytest", "-q", test_node_id]
    started = time.monotonic()
    try:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except (FileNotFoundError, OSError) as exc:
        return TestExecutionResult(
            test_node_id=test_node_id,
            stderr=f"could not start pytest: {exc}",
            duration_seconds=time.monotonic() - started,
            collection_error=False,
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        stdout, stderr = process.communicate()
        return TestExecutionResult(
            test_node_id=test_node_id,
            exit_code=124,
            stdout=stdout or (exc.stdout or ""),
            stderr=stderr or (exc.stderr or ""),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            passed=False,
        )
    return TestExecutionResult(
        test_node_id=test_node_id,
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
        passed=process.returncode == 0,
        collection_error=process.returncode == 2,
    )


execute_targeted_test = run_targeted_test
