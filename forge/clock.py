"""Deterministic clock: every timestamp comes from MAKEFUN_TRIAL_NOW (or a
fixed base) plus a sequence offset. No wall clock is ever read, so two runs
over the same request sequence produce byte-identical state.
"""

import os
from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self):
        raw = os.environ.get("MAKEFUN_TRIAL_NOW", "")
        base = None
        if raw:
            try:
                base = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                base = None
        if base is None:
            base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        self.base = base
        self.seq = 0

    def _tick(self):
        self.seq += 1
        return self.base + timedelta(seconds=self.seq)

    def now_iso(self):
        return self._tick().strftime("%Y-%m-%dT%H:%M:%SZ")

    def now_ts(self):
        return int(self._tick().timestamp())

    def iso_of_ts(self, ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
