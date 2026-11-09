"""Bounded subprocesses. Never log command arguments, environment or stderr."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
from .errors import BackupError
from .limits import poll_capacity

MAX_CAPTURE = 32 * 1024 * 1024

@dataclass
class Result:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


def run(argv: list[str], *, env: dict | None = None, cwd: Path | None = None,
        input_data: bytes | None = None, timeout: int = 900,
        stdout_path: Path | None = None, input_path: Path | None = None, check: bool = True) -> Result:
    if not argv or any(not isinstance(x, str) or "\0" in x for x in argv):
        raise BackupError("invalid_command", "Invalid subprocess argument")
    if timeout <= 0:
        raise BackupError("invalid_timeout", "Subprocess timeout must be positive")
    if input_data is not None and input_path is not None:
        raise BackupError("invalid_input", "Subprocess input source is ambiguous")
    # File-backed capture bounds RAM. Callers place export files in quota-limited scratch.
    with tempfile.TemporaryFile() as captured, tempfile.TemporaryFile() as errors:
        output = None
        source = None
        try:
            if input_path is not None:
                fd = os.open(input_path, os.O_RDONLY | os.O_NOFOLLOW)
                source = os.fdopen(fd, "rb")
            if stdout_path is not None:
                fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                output = os.fdopen(fd, "wb")
            try:
                p = subprocess.Popen(argv, stdin=source if source else subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                     stdout=output if output else captured, stderr=errors,
                                     cwd=cwd, env=env, start_new_session=True)
            except OSError:
                raise BackupError("tool_unavailable", "Required executable could not be started") from None
            try:
                # Poll bounded file-backed diagnostics while the child is active.
                # Streaming archive input is a file; small SQL/stdin metadata uses communicate.
                deadline = time.monotonic() + timeout
                first = True
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise subprocess.TimeoutExpired(argv[0], timeout)
                    try:
                        p.communicate(input=input_data if first else None, timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                        poll_capacity()
                        if os.fstat(captured.fileno()).st_size > MAX_CAPTURE or os.fstat(errors.fileno()).st_size > MAX_CAPTURE:
                            raise BackupError("output_limit", "Subprocess diagnostics exceeded the output limit")
            except BaseException as exc:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=5)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    pass
                # Always kill any grandchildren left in the group, even if its leader exited.
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise BackupError("command_timeout", "Subprocess exceeded its wall-clock deadline") from None
                raise
            if output:
                output.flush()
                os.fsync(output.fileno())
            captured.seek(0); errors.seek(0)
            stdout = b"" if output else captured.read(MAX_CAPTURE + 1)
            stderr = errors.read(MAX_CAPTURE + 1)
            if len(stdout) > MAX_CAPTURE or len(stderr) > MAX_CAPTURE:
                raise BackupError("output_limit", "Subprocess metadata exceeded the output limit")
            result = Result(p.returncode, stdout, stderr)
            if check and p.returncode:
                raise BackupError("command_failed", "Subprocess failed; no success was recorded")
            return result
        finally:
            if output:
                output.close()
            if source:
                source.close()
