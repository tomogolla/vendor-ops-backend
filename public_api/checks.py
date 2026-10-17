"""System checks for the public application form."""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from .turnstile import is_test_secret


@register(Tags.security)
def public_form_check(app_configs, **kwargs):
    problems = []
    if settings.PUBLIC_APPLICATIONS_ENABLED and not settings.DEBUG:
        secret = settings.TURNSTILE_SECRET_KEY
        if not secret:
            problems.append(Error(
                'PUBLIC_APPLICATIONS_ENABLED is on in production but TURNSTILE_SECRET_KEY is not set.',
                hint='Set TURNSTILE_SECRET_KEY in the server environment, or turn the form off.', id='public_api.E001',
            ))
        elif is_test_secret(secret):
            problems.append(Warning(
                'TURNSTILE_SECRET_KEY is a Cloudflare test key; bots are not being checked.',
                hint="Use the widget's real secret key in production.", id='public_api.W001',
            ))
        if not settings.TURNSTILE_EXPECTED_HOSTNAMES:
            problems.append(Error(
                'TURNSTILE_EXPECTED_HOSTNAMES is empty, so tokens from any site would be accepted.',
                hint='Set it to the public form host, e.g. ops.thegoodflea.com.', id='public_api.E002',
            ))
        if not settings.TURNSTILE_EXPECTED_ACTION:
            problems.append(Error(
                'TURNSTILE_EXPECTED_ACTION is empty, so tokens for any action would be accepted.',
                hint='Set it to the action the page uses (apply).', id='public_api.E003',
            ))
    return problems
