from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from communications.sending import send_test_email


class Command(BaseCommand):
    help = (
        'Send one test email to ADDRESS through the real sending path (configured provider, sending mode, '
        'redirect mailbox and daily limit). In disabled mode nothing is sent; in redirect mode it goes to the '
        'redirect mailbox. The message is recorded like any other (template key test_email).'
    )

    def add_arguments(self, parser):
        parser.add_argument('address', help='Where to send the test email.')

    def handle(self, *args, address, **options):
        try:
            result = send_test_email(address)
        except ValidationError:
            raise CommandError(f'{address!r} is not a valid email address.') from None
        if result.outcome != 'sent':
            # A non-zero exit, so scripts and deploy checks notice.
            raise CommandError(result.detail)
        self.stdout.write(self.style.SUCCESS(result.detail))
        if result.message is not None:
            self.stdout.write(f'Recorded as email message #{result.message.pk}.')
