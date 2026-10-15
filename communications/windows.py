"""Send-window arithmetic in the configured business time zone (never the server's)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def in_window(moment: datetime, tz: str, start: time, end: time) -> bool:
    local = moment.astimezone(ZoneInfo(tz))
    return start <= local.time() < end


def next_send_time(moment: datetime, tz: str, start: time, end: time) -> datetime:
    """`moment` if it is inside the daily window, otherwise the next window opening (today or
    tomorrow), as an aware datetime."""
    zone = ZoneInfo(tz)
    local = moment.astimezone(zone)
    if start <= local.time() < end:
        return moment
    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    return datetime.combine(day, start, tzinfo=zone)
