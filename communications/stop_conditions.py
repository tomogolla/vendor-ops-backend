"""Stop conditions: checks re-run by the worker immediately before a queued message is sent.

A condition receives the EmailMessage and returns a short reason string to stop it (the message
is skipped with that reason) or None to let it go. Later slices register conditions such as
"application is still unpaid" for payment reminders:

    from communications import stop_conditions

    @stop_conditions.register('payment_reminder')
    def application_paid(message):
        return 'Already paid' if message.application and message.application.first_paid_at else None

Slice 1 registers none. Conditions for '*' apply to every template.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

StopCondition = Callable[[object], str | None]
ALL_TEMPLATES = '*'
_registry: dict[str, list[tuple[str, StopCondition]]] = {}


def register(template_key: str = ALL_TEMPLATES, name: str | None = None):
    def decorator(condition: StopCondition) -> StopCondition:
        _registry.setdefault(template_key, []).append((name or condition.__name__, condition))
        return condition
    return decorator


def unregister(template_key: str, condition: StopCondition):
    _registry[template_key] = [item for item in _registry.get(template_key, []) if item[1] is not condition]


@contextmanager
def registered(template_key: str, condition: StopCondition, name: str | None = None) -> Iterator[None]:
    """Registers a condition for the duration of a block (tests, one-off scripts)."""
    register(template_key, name)(condition)
    try:
        yield
    finally:
        unregister(template_key, condition)


def conditions_for(template_key: str) -> list[tuple[str, StopCondition]]:
    return [*_registry.get(ALL_TEMPLATES, []), *_registry.get(template_key, [])]


def stop_reason(message) -> tuple[str, str] | None:
    """(condition name, reason) for the first condition that stops the message, else None.
    A condition that raises stops the message: when in doubt, do not send."""
    for name, condition in conditions_for(message.template_key):
        try:
            reason = condition(message)
        except Exception:  # noqa: BLE001 - a broken check must not let mail through
            logger.exception('Email stop condition %s failed for message %s', name, message.pk)
            return name, 'The stop condition could not be evaluated.'
        if reason:
            return name, str(reason)[:300]
    return None
