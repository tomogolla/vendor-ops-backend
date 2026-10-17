"""Abuse limits for the public form beyond the per-IP throttle: a per-email daily limit and a
daily cap on website applications. Both live outside the request body logs."""

import hashlib
import hmac
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache

from vendors.models import Application

DAY_SECONDS = 24 * 60 * 60


def _email_key(email: str) -> str:
    # Keyed hash: the cache never holds the address itself.
    digest = hmac.new(settings.SECRET_KEY.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()
    return f'public_apply:email:{digest}'


def email_limit_reached(email: str) -> bool:
    now = time.time()
    history = [stamp for stamp in cache.get(_email_key(email), []) if stamp > now - DAY_SECONDS]
    return len(history) >= settings.PUBLIC_APPLICATIONS_EMAIL_LIMIT_PER_DAY


def record_email_submission(email: str) -> None:
    now = time.time()
    key = _email_key(email)
    history = [stamp for stamp in cache.get(key, []) if stamp > now - DAY_SECONDS]
    cache.set(key, [*history, now], DAY_SECONDS)


def daily_cap_reached(source: str) -> bool:
    """Applications from the website created since midnight in the business time zone."""
    zone = ZoneInfo(settings.BUSINESS_HOURS['timezone'])
    midnight = datetime.combine(datetime.now(zone).date(), dtime.min, tzinfo=zone)
    created = Application.objects.filter(source=source, submitted_at__gte=midnight).count()
    return created >= settings.PUBLIC_APPLICATIONS_DAILY_CAP
