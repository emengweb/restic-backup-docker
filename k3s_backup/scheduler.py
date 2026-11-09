"""Cron-compatible matching, preserving project windows and timezone."""
from __future__ import annotations
from datetime import datetime
import re
from .errors import BackupError


def values(text, lo, hi, dow=False):
    result = set()
    for item in text.split(","):
        part, sep, step = item.partition("/")
        try: stride = int(step) if sep else 1
        except ValueError: raise BackupError("invalid_cron", "Cron step is invalid") from None
        if stride <= 0: raise BackupError("invalid_cron", "Cron step must be positive")
        if part == "*": start, end = lo, hi
        elif "-" in part:
            try: start, end = map(int, part.split("-"))
            except ValueError: raise BackupError("invalid_cron", "Cron range is invalid") from None
        else:
            try: start = int(part); end = hi if sep else start
            except ValueError: raise BackupError("invalid_cron", "Cron value is invalid") from None
        if start < lo or end > hi or start > end: raise BackupError("invalid_cron", "Cron value is out of range")
        result.update(range(start, end + 1, stride))
    return {0 if dow and x == 7 else x for x in result}


class Cron:
    def __init__(self, expression):
        parts = expression.split()
        if len(parts) != 5 or any(not re.fullmatch(r"[0-9*/,\-]+", x) for x in parts):
            raise BackupError("invalid_cron", "Cron must contain five numeric fields")
        self.parts = parts
        self.fields = [values(p, lo, hi, i == 4) for i, (p, lo, hi) in enumerate(zip(parts, [0,0,1,1,0], [59,23,31,12,7]))]

    def matches(self, now: datetime):
        minute, hour, day, month, dow = self.fields
        a, b = now.day in day, (now.weekday() + 1) % 7 in dow
        # Vixie cron: restricted DOM/DOW OR, wildcard fields require intersection.
        days = (a or b) if not self.parts[2].startswith("*") and not self.parts[4].startswith("*") else (a and b)
        return now.minute in minute and now.hour in hour and now.month in month and days


def in_window(window, now):
    start, end = map(int, window.split("-"))
    return start <= now.hour <= end if start <= end else now.hour >= start or now.hour <= end
