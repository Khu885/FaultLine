"""Targeted pytest execution with bounded subprocess handling."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .models import TestExecutionResult


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


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


def _execution_environment(root: Path) -> dict[str, str]:
    """Provide a minimal environment and prevent obvious secret leakage."""
    blocked_tokens = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "PRIVATE_KEY")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(token in key.upper() for token in blocked_tokens)
    }
    paths = [str(root), str(root.parent)]
    existing = environment.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


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
            env=_execution_environment(root),
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
            stdout=_text(stdout or exc.stdout),
            stderr=_text(stderr or exc.stderr),
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
