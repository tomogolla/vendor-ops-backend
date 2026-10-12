from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


@dataclass(frozen=True)
class BusinessHours:
    tz: ZoneInfo
    days: frozenset[int]
    open: time
    close: time

    @classmethod
    def from_settings(cls) -> 'BusinessHours':
        config = settings.BUSINESS_HOURS
        return cls(
            tz=ZoneInfo(config['timezone']),
            days=frozenset(config['days']),
            open=time.fromisoformat(config['open']),
            close=time.fromisoformat(config['close']),
        )

    def _at(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=self.tz)

    def is_open(self, local: datetime) -> bool:
        return local.weekday() in self.days and self.open <= local.time() < self.close

    def next_opening(self, local: datetime) -> datetime:
        day = local.date()
        if local.weekday() in self.days and local.time() < self.open:
            return self._at(day, self.open)
        for offset in range(1, 15):
            candidate = day + timedelta(days=offset)
            if candidate.weekday() in self.days:
                return self._at(candidate, self.open)
        raise ValueError('BUSINESS_DAYS must contain at least one weekday.')

    def add_business_hours(self, local: datetime, hours: float) -> datetime:
        remaining = timedelta(hours=hours)
        cursor = local if self.is_open(local) else self.next_opening(local)
        while True:
            available = self._at(cursor.date(), self.close) - cursor
            if remaining <= available:
                return cursor + remaining
            remaining -= available
            cursor = self.next_opening(self._at(cursor.date(), self.close))


def discovery_call_due_at(now: datetime | None = None, hours: BusinessHours | None = None) -> datetime:
    """Within N business hours; when created outside business hours, the next opening."""
    hours = hours or BusinessHours.from_settings()
    local = (now or timezone.now()).astimezone(hours.tz)
    if not hours.is_open(local):
        return hours.next_opening(local)
    return hours.add_business_hours(local, settings.DISCOVERY_CALL_DUE_BUSINESS_HOURS)
