"""Email transports behind one small interface, chosen by settings.EMAIL_PROVIDER.

- `console`: Django's EMAIL_BACKEND (the console locally; Django's test runner swaps in the
  in-memory backend). Never reaches a real mail server.
- `smtp_relay`: Google Workspace SMTP relay (smtp-relay.gmail.com:587, STARTTLS). Production only;
  needs the droplet's IP allowlisted in Workspace and SPF/DKIM/DMARC for the sender domain.
- `resend`: Resend's API through django-anymail; needs EMAIL_RESEND_API_KEY and a verified domain.
"""

import smtplib
import socket
from dataclasses import dataclass, field
from email.utils import formataddr, make_msgid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend


@dataclass(frozen=True)
class OutboundEmail:
    """What actually goes on the wire (in redirect mode, `to_email` is the redirect mailbox)."""
    to_name: str
    to_email: str
    from_name: str
    from_email: str
    reply_to: str
    subject: str
    html_body: str
    text_body: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider: str
    message_id: str


class EmailSendError(Exception):
    """A delivery failure with a safe, short code and detail (never credentials)."""

    def __init__(self, code: str, detail: str, *, transient: bool, retry_later: bool = False):
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail[:500]
        self.transient = transient
        # A rate-limit reply: try again later without using up a retry attempt.
        self.retry_later = retry_later


class EmailProvider:
    name = 'base'

    def send(self, email: OutboundEmail) -> SendResult:
        raise NotImplementedError

    def _build(self, email: OutboundEmail, connection, *, set_message_id: bool = True) -> tuple[EmailMultiAlternatives, str]:
        """Builds the Django message. With set_message_id=False the provider assigns the id."""
        message_id = ''
        headers = dict(email.headers)
        if set_message_id:
            domain = email.from_email.rsplit('@', 1)[-1] if '@' in email.from_email else 'vendor-ops.invalid'
            message_id = make_msgid(domain=domain)
            headers = {'Message-ID': message_id, **headers}
        message = EmailMultiAlternatives(
            subject=email.subject,
            body=email.text_body,
            from_email=formataddr((email.from_name, email.from_email)),
            to=[formataddr((email.to_name, email.to_email))],
            reply_to=[email.reply_to] if email.reply_to else None,
            headers=headers,
            connection=connection,
        )
        message.attach_alternative(email.html_body, 'text/html')
        return message, message_id


class DjangoBackendProvider(EmailProvider):
    """Development and CI: whatever Django's EMAIL_BACKEND is (console or in-memory)."""

    name = 'console'

    def send(self, email: OutboundEmail) -> SendResult:
        message, message_id = self._build(email, get_connection(fail_silently=False))
        message.send(fail_silently=False)
        return SendResult(self.name, message_id)


class SmtpRelayProvider(EmailProvider):
    """Google Workspace SMTP relay. Authentication is optional: the relay can trust the droplet's
    allowlisted IP, or require SMTP AUTH with a Workspace account, depending on the admin setup."""

    name = 'smtp_relay'

    def __init__(self, config: dict | None = None):
        self.config = config or settings.EMAIL_SMTP
        if not self.config.get('host'):
            raise ImproperlyConfigured('EMAIL_SMTP_HOST is not set.')

    def send(self, email: OutboundEmail) -> SendResult:
        connection = SmtpBackend(
            host=self.config['host'],
            port=self.config['port'],
            username=self.config.get('username') or None,
            password=self.config.get('password') or None,
            use_tls=self.config.get('use_tls', True),
            timeout=self.config.get('timeout', 20),
            fail_silently=False,
        )
        message, message_id = self._build(email, connection)
        try:
            sent = message.send(fail_silently=False)
        except smtplib.SMTPAuthenticationError as error:
            raise EmailSendError('auth_failed', f'SMTP authentication failed ({error.smtp_code}).', transient=False) from None
        except smtplib.SMTPRecipientsRefused as error:
            codes = sorted({code for code, _ in error.recipients.values()})
            transient = all(400 <= code < 500 for code in codes)
            raise EmailSendError('recipient_refused', f'Recipient refused ({", ".join(map(str, codes))}).', transient=transient) from None
        except smtplib.SMTPSenderRefused as error:
            raise EmailSendError('sender_refused', f'Sender refused ({error.smtp_code}). Check the relay allowlist and sender.', transient=400 <= error.smtp_code < 500) from None
        except smtplib.SMTPResponseException as error:
            raise EmailSendError('smtp_error', f'SMTP error {error.smtp_code}.', transient=400 <= error.smtp_code < 500) from None
        except (smtplib.SMTPException, socket.timeout, OSError) as error:
            raise EmailSendError('connection_error', f'Could not reach the mail relay ({type(error).__name__}).', transient=True) from None
        if not sent:
            raise EmailSendError('not_accepted', 'The relay did not accept the message.', transient=True)
        return SendResult(self.name, message_id)


class ResendProvider(EmailProvider):
    """Resend (resend.com) through django-anymail's Resend backend. Resend assigns the message id,
    which its delivery webhooks report back (see webhooks.py). Tests swap EMAIL_RESEND_BACKEND for
    Anymail's test backend, so they never call Resend."""

    name = 'resend'

    def send(self, email: OutboundEmail) -> SendResult:
        from anymail.exceptions import AnymailAPIError, AnymailConfigurationError, AnymailRecipientsRefused

        try:
            connection = get_connection(settings.EMAIL_RESEND_BACKEND, fail_silently=False)
            message, _ = self._build(email, connection, set_message_id=False)
            message.send(fail_silently=False)
        except AnymailRecipientsRefused:
            raise EmailSendError('recipient_refused', 'Resend refused the recipient.', transient=False) from None
        except AnymailConfigurationError as error:
            raise EmailSendError('configuration_error', f'Resend is not configured: {type(error).__name__}.', transient=False) from None
        except AnymailAPIError as error:
            status = error.status_code
            if status == 429:
                raise EmailSendError('rate_limited', 'Resend rate limit (HTTP 429); will try later.', transient=True,
                                     retry_later=True) from None
            if status in (401, 403):
                raise EmailSendError('auth_failed', f'Resend rejected the API key (HTTP {status}).', transient=False) from None
            if status is None or status >= 500:
                detail = f'Resend unavailable (HTTP {status}).' if status else 'Could not reach Resend.'
                raise EmailSendError('provider_unavailable', detail, transient=True) from None
            raise EmailSendError('provider_rejected', f'Resend refused the message (HTTP {status}).', transient=False) from None
        status = getattr(message, 'anymail_status', None)
        message_id = status.message_id if status is not None else None
        if message_id is None or message_id == '':
            raise EmailSendError('not_accepted', 'Resend did not return a message id.', transient=True)
        return SendResult(self.name, str(message_id))


PROVIDERS = {'console': DjangoBackendProvider, 'smtp_relay': SmtpRelayProvider, 'resend': ResendProvider}


def get_provider() -> EmailProvider:
    try:
        return PROVIDERS[settings.EMAIL_PROVIDER]()
    except KeyError:
        raise ImproperlyConfigured(f'Unknown EMAIL_PROVIDER {settings.EMAIL_PROVIDER!r}.') from None
