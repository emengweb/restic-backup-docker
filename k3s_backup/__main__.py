"""Credential-free CLI output; no stack traces or raw tool diagnostics."""
import argparse
import json
import sys
from .config import Config
from .errors import BackupError
from .runner import Runner, emit


def main(argv=None):
    p = argparse.ArgumentParser(description="K3s automatic latest backup/recovery")
    p.add_argument("command", choices=["backup", "restore-init", "check", "prune", "health", "schedule"], nargs="?", default="schedule")
    p.add_argument("--apply", action="store_true", help="Apply authorized retention (otherwise plan only)")
    args = p.parse_args(argv)
    try:
        runner = Runner(Config.from_env())
        methods = {"backup": runner.backup, "restore-init": runner.restore, "check": runner.check,
                   "prune": lambda: runner.prune(args.apply), "health": runner.health, "schedule": runner.schedule}
        result = methods[args.command]()
        if result is not None: emit(args.command, **result)
        return 0
    except BackupError as e:
        emit("error", code=e.code, message=e.message)
        return 1
    except KeyboardInterrupt:
        emit("interrupted"); return 130
    except Exception:
        emit("error", code="internal_error", message="Operation failed; no successful result was recorded")
        return 1

if __name__ == "__main__":
    sys.exit(main())
