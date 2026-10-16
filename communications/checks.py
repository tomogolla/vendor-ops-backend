"""System checks for the email configuration (run by `manage.py check` and on every deploy)."""

from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.checks import Error, Warning, register

from .models import GLOBAL_MODE_ORDER
from .providers import PROVIDERS


@register()
def email_settings_check(app_configs, **kwargs):
    problems = []
    if settings.EMAIL_PROVIDER not in PROVIDERS:
        problems.append(Error(f'EMAIL_PROVIDER must be one of: {", ".join(PROVIDERS)}.', id='communications.E001'))
    if settings.EMAIL_GLOBAL_MODE not in GLOBAL_MODE_ORDER:
        problems.append(Error('EMAIL_GLOBAL_MODE must be disabled, redirect or enabled.', id='communications.E002'))
    try:
        ZoneInfo(settings.EMAIL_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(Error('EMAIL_TIMEZONE must be an IANA time zone, e.g. America/New_York.', id='communications.E003'))
    try:
        if time.fromisoformat(settings.EMAIL_SEND_WINDOW_START) >= time.fromisoformat(settings.EMAIL_SEND_WINDOW_END):
            problems.append(Error('EMAIL_SEND_WINDOW_END must be after EMAIL_SEND_WINDOW_START.', id='communications.E004'))
    except ValueError:
        problems.append(Error('EMAIL_SEND_WINDOW_START/END must look like 09:00.', id='communications.E004'))
    if settings.EMAIL_PROVIDER == 'smtp_relay':
        if not settings.EMAIL_FROM_ADDRESS:
            problems.append(Error('EMAIL_FROM_ADDRESS is required with the smtp_relay provider.', id='communications.E005'))
        if not settings.EMAIL_SMTP.get('use_tls'):
            problems.append(Warning('EMAIL_SMTP_USE_TLS is off; the relay should use TLS.', id='communications.W001'))
    if settings.EMAIL_PROVIDER == 'resend':
        if not settings.ANYMAIL.get('RESEND_API_KEY'):
            problems.append(Error('EMAIL_RESEND_API_KEY is required with the resend provider.', id='communications.E006'))
        if not settings.EMAIL_FROM_ADDRESS:
            problems.append(Error('EMAIL_FROM_ADDRESS is required with the resend provider.', id='communications.E007'))
        if not settings.ANYMAIL.get('RESEND_SIGNING_SECRET'):
            problems.append(Warning(
                'EMAIL_RESEND_WEBHOOK_SECRET is empty: the delivery webhook refuses every call, so delivered, '
                'bounced and complained statuses will not be recorded.', id='communications.W003',
            ))
    if settings.EMAIL_DAILY_LIMIT < 0:
        problems.append(Error('EMAIL_DAILY_LIMIT must be 0 (no limit) or more.', id='communications.E008'))
    if settings.EMAIL_GLOBAL_MODE == 'redirect' and not settings.EMAIL_REDIRECT_TO:
        problems.append(Warning(
            'EMAIL_GLOBAL_MODE is redirect but EMAIL_REDIRECT_TO is empty; set it (or the admin redirect address) '
            'or nothing will be sent.', id='communications.W002',
        ))
    return problems
