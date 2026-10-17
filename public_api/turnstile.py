"""Cloudflare Turnstile server-side verification (siteverify). Fails closed: anything other than a
confirmed success, for the expected hostname and action, refuses the submission.

There is no bypass: without TURNSTILE_SECRET_KEY nothing is accepted, in development too (use
Cloudflare's test secret there). The only relaxation is for Cloudflare's own test secrets with DEBUG
on, whose responses carry placeholder hostnames and actions: then those two checks are skipped."""

import ipaddress
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
MAX_TOKEN_LENGTH = 2048
# Cloudflare's documented dummy secrets (always pass / always fail / token already spent).
TEST_SECRETS = (
    '1x0000000000000000000000000000000AA',
    '2x0000000000000000000000000000000AA',
    '3x0000000000000000000000000000000AA',
)
# Error codes that mean our configuration or Cloudflare is at fault, not the visitor.
UNAVAILABLE_CODES = {'missing-input-secret', 'invalid-input-secret', 'internal-error', 'bad-request'}


class TurnstileFailed(Exception):
    """The visitor did not pass: missing, invalid, expired or reused token, or a token issued for
    another hostname or action."""


class TurnstileUnavailable(Exception):
    """Verification could not run (no secret, Turnstile unreachable, misconfigured or erroring)."""


def reliable_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address((value or '').strip()))
    except ValueError:
        return None


def is_test_secret(secret: str) -> bool:
    return secret in TEST_SECRETS


def verify(token: str, remote_ip: str | None = None) -> None:
    secret = settings.TURNSTILE_SECRET_KEY
    if not secret:
        logger.error('Turnstile verification refused: TURNSTILE_SECRET_KEY is not set')
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
    if not isinstance(result, dict):
        raise TurnstileUnavailable('unexpected-response')

    if result.get('success') is not True:
        codes = [str(code) for code in result.get('error-codes') or []]
        if UNAVAILABLE_CODES & set(codes):
            logger.error('Turnstile verification could not run: %s', ','.join(codes))
            raise TurnstileUnavailable(','.join(codes))
        # invalid-input-response (invalid or expired), timeout-or-duplicate (reused), missing-input-response.
        logger.info('Turnstile verification failed: %s', ','.join(codes) or 'no error code')
        raise TurnstileFailed(','.join(codes) or 'failed')

    if settings.DEBUG and is_test_secret(secret):
        return  # test-key responses carry placeholder hostnames and actions
    hostname = str(result.get('hostname') or '')
    if hostname not in settings.TURNSTILE_EXPECTED_HOSTNAMES:
        logger.warning('Turnstile token refused: issued for an unexpected hostname')
        raise TurnstileFailed('hostname-mismatch')
    expected_action = settings.TURNSTILE_EXPECTED_ACTION
    if expected_action and str(result.get('action') or '') != expected_action:
        logger.warning('Turnstile token refused: unexpected action')
        raise TurnstileFailed('action-mismatch')
