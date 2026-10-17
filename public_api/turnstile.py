"""Cloudflare Turnstile server-side verification (siteverify). Fails closed: anything other than a
confirmed success refuses the submission."""

import ipaddress
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
MAX_TOKEN_LENGTH = 2048


class TurnstileFailed(Exception):
    """The visitor did not pass (missing, invalid, expired or reused token)."""


class TurnstileUnavailable(Exception):
    """Verification could not run (no secret in production, Turnstile unreachable or erroring)."""


def reliable_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address((value or '').strip()))
    except ValueError:
        return None


def verify(token: str, remote_ip: str | None = None) -> None:
    secret = settings.TURNSTILE_SECRET_KEY
    if not secret:
        if settings.DEBUG:
            logger.warning('Turnstile verification skipped: DEBUG is on and TURNSTILE_SECRET_KEY is not set')
            return
        raise TurnstileUnavailable('TURNSTILE_SECRET_KEY is not set')
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise TurnstileFailed('missing-or-malformed-token')
    data = {'secret': secret, 'response': token}
    if remote_ip:
        data['remoteip'] = remote_ip
    try:
        response = requests.post(settings.TURNSTILE_VERIFY_URL, data=data, timeout=settings.TURNSTILE_TIMEOUT_SECONDS)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        logger.warning('Turnstile unreachable: %s', type(error).__name__)
        raise TurnstileUnavailable('unreachable') from None
    if result.get('success') is True:
        return
    codes = [str(code) for code in result.get('error-codes') or []]
    if {'missing-input-secret', 'invalid-input-secret', 'internal-error'} & set(codes):
        logger.error('Turnstile verification could not run: %s', ','.join(codes))
        raise TurnstileUnavailable(','.join(codes))
    logger.info('Turnstile verification failed: %s', ','.join(codes) or 'no error code')
    raise TurnstileFailed(','.join(codes))
