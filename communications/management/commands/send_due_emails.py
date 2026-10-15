import logging

from django.core.management.base import BaseCommand, CommandError

from communications.sending import process_due_emails

logger = logging.getLogger('communications.sending')
MAX_LIMIT = 500


class Command(BaseCommand):
    help = (
        'Send queued emails that are due, inside the send window and allowed by the global sending mode. '
        'Safe to run repeatedly (the systemd timer runs it every minute).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would happen; change nothing, send nothing.')
        parser.add_argument('--limit', type=int, default=50, help=f'Messages per run (1-{MAX_LIMIT}, default 50).')

    def handle(self, *args, dry_run=False, limit=50, **options):
        if not 1 <= limit <= MAX_LIMIT:
            raise CommandError(f'--limit must be between 1 and {MAX_LIMIT}.')
        summary = process_due_emails(limit=limit, dry_run=dry_run)
        # One line per run, through logging (the console locally, the journal under systemd).
        logger.info(summary.line())
