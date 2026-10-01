"""
Wall-clock-aligned Beat schedule with a fixed second offset.

Celery's interval schedules count from Beat start, so every 60 s entry fires in the same
second (and the 300 s one joins them every fifth minute); crontab cannot express seconds.
offset_schedule(period, offset) is due at UTC times t where t % period == offset, e.g.

    offset_schedule(60, 0)    every minute at :00
    offset_schedule(60, 20)   every minute at :20
    offset_schedule(300, 40)  every 5 minutes (minute 0, 5, 10, ...) at :40

It is a schedule (used by the standard Beat scheduler), not a custom scheduler. A slot
that Beat missed (e.g. while restarting) is not replayed: the entry fires once, at the
first check after the slot, then returns to the aligned cadence.
"""

import math
from datetime import datetime, timezone

from celery.schedules import BaseSchedule, schedstate


class offset_schedule(BaseSchedule):
    def __init__(self, period, offset=0, nowfun=None, app=None):
        if period <= 0 or not 0 <= offset < period:
            raise ValueError("offset_schedule needs period > 0 and 0 <= offset < period")
        self.period = float(period)
        self.offset = float(offset)
        super().__init__(nowfun=nowfun, app=app)

    @property
    def seconds(self):
        return self.period

    def next_slot_after(self, moment):
        """First aligned slot strictly after `moment` (an aware datetime)."""
        ts = moment.timestamp()
        n = math.floor((ts - self.offset) / self.period) + 1
        return datetime.fromtimestamp(n * self.period + self.offset, tz=timezone.utc)

    def remaining_estimate(self, last_run_at):
        last_run_at = self.maybe_make_aware(last_run_at)
        now = self.maybe_make_aware(self.now())
        return self.next_slot_after(last_run_at) - now

    def is_due(self, last_run_at):
        last_run_at = self.maybe_make_aware(last_run_at)
        now = self.maybe_make_aware(self.now())
        due_at = self.next_slot_after(last_run_at)

        if now >= due_at:
            # Run now; check again at the slot after this one.
            return schedstate(is_due=True, next=(self.next_slot_after(now) - now).total_seconds())
        return schedstate(is_due=False, next=(due_at - now).total_seconds())

    def __repr__(self):
        return f"<offset_schedule: every {self.period:g}s at +{self.offset:g}s>"

    def __eq__(self, other):
        if isinstance(other, offset_schedule):
            return (self.period, self.offset) == (other.period, other.offset)
        return NotImplemented

    def __reduce__(self):
        # Beat pickles entries into its schedule file.
        return self.__class__, (self.period, self.offset, self.nowfun)

