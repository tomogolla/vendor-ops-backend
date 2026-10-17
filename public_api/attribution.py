"""Where a public submission came from, stored in LeadIntakeEvent.metadata.

Only what the page reports (UTM tags, click ids, referrer, landing path), a truncated user agent
and the consent the applicant gave. Every value is cleaned and truncated. The visitor's IP address
is never stored."""

from django.utils import timezone

from .text import clean_line

# Field -> maximum length kept.
ATTRIBUTION_FIELDS = {
    'utm_source': 150, 'utm_medium': 150, 'utm_campaign': 150, 'utm_content': 150, 'utm_term': 150,
    'fbclid': 255, 'gclid': 255, 'referrer': 500, 'landing_path': 300,
}
USER_AGENT_LENGTH = 300
CONSENT_VERSION_LENGTH = 50


def clean_attribution(raw) -> dict:
    """Known fields only, as clean strings; unknown keys and non-text values are dropped."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for name, limit in ATTRIBUTION_FIELDS.items():
        value = raw.get(name)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = clean_line(value, limit)
            if text:
                cleaned[name] = text
    return cleaned


def intake_metadata(*, attribution, user_agent: str, consent_version: str, accepted_at=None) -> dict:
    return {
        **clean_attribution(attribution),
        'user_agent': clean_line(user_agent, USER_AGENT_LENGTH),
        'consent': {
            'text_version': clean_line(consent_version, CONSENT_VERSION_LENGTH),
            'accepted_at': (accepted_at or timezone.now()).isoformat(),
        },
    }
