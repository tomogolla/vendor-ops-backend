"""System checks for the public application form."""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

TEST_SECRET_PREFIXES = ('1x0000000000000000000000000000000', '2x0000000000000000000000000000000', '3x0000000000000000000000000000000')


@register(Tags.security, deploy=False)
def public_form_check(app_configs, **kwargs):
    problems = []
    if settings.PUBLIC_APPLICATIONS_ENABLED and not settings.DEBUG:
        secret = settings.TURNSTILE_SECRET_KEY
        if not secret:
            problems.append(Error(
                'PUBLIC_APPLICATIONS_ENABLED is on in production but TURNSTILE_SECRET_KEY is not set.',
                hint='Set TURNSTILE_SECRET_KEY in the server environment, or turn the form off.', id='public_api.E001',
            ))
        elif secret.startswith(TEST_SECRET_PREFIXES):
            problems.append(Warning(
                'TURNSTILE_SECRET_KEY is a Cloudflare test key; bots are not being checked.',
                hint="Use the widget's real secret key in production.", id='public_api.W001',
            ))
    return problems
