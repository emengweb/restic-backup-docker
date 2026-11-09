"""Whole-job deadlines and per-process file-size limits, restored after the job."""
from contextlib import contextmanager
import resource
import signal
import threading
import contextvars
import os
import stat
from pathlib import Path

_BUDGET = contextvars.ContextVar("backup_budget", default=None)

def poll_capacity():
    value = _BUDGET.get()
    if value is None: return
    paths, limit, reserve = value
    seen=set(); total=0
    for path in paths:
        if not path.exists(): continue
        fs=os.statvfs(path)
        if fs.f_bavail * fs.f_frsize < reserve:
            raise BackupError("insufficient_space", "Backup or restore scratch reached its free-space reserve")
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try: s=os.lstat(Path(parent)/name)
                except FileNotFoundError: continue
                key=(s.st_dev,s.st_ino)
                if stat.S_ISREG(s.st_mode) and key not in seen:
                    total+=s.st_size; seen.add(key)
                    if total > limit: raise BackupError("size_limit", "Aggregate backup/restore scratch exceeded its budget")

@contextmanager
def capacity_budget(paths, limit, reserve):
    token=_BUDGET.set(([Path(p) for p in paths],limit,reserve))
    try:
        poll_capacity();yield
    finally:_BUDGET.reset(token)
from .errors import BackupError

@contextmanager
def job_budget(seconds: int, max_file_bytes: int):
    if threading.current_thread() is not threading.main_thread():
        raise BackupError("threaded_job_unsupported", "Jobs must execute in the main process")
    oldhandler = signal.getsignal(signal.SIGALRM)
    oldlimit = resource.getrlimit(resource.RLIMIT_FSIZE)
    oldtimer = signal.getitimer(signal.ITIMER_REAL)
    def expired(*_):
        raise BackupError("job_timeout", "Job exceeded its total wall-clock budget")
    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        soft = max_file_bytes if oldlimit[0] == resource.RLIM_INFINITY else min(oldlimit[0], max_file_bytes)
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, oldlimit[1]))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *oldtimer)
        signal.signal(signal.SIGALRM, oldhandler)
        resource.setrlimit(resource.RLIMIT_FSIZE, oldlimit)
