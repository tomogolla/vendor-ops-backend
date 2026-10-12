import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from tasks.models import Task
from tasks.services import log_call
from vendors import services
from vendors.models import Vendor

DEMO_VENDORS = [
    # (vendor fields, path of display statuses after registration, call outcome or None)
    ({'brand_name': 'Acme Corporation', 'first_name': 'Cy', 'last_name': 'Ganderton', 'email': 'cy@acme.example.com',
      'phone': '(555) 123-4567', 'instagram': '@acme', 'website': 'https://acme.example.com',
      'category': 'Arts, Prints & Stickers', 'lead_source': 'Meta ads', 'tags': ['VIP', 'Tier-1'],
      'description': 'Independent artwork, prints, and stickers.', 'commitment_level': 'Full time'},
     ['applied', 'in_review', 'approved'], 'Interested'),
    ({'brand_name': 'Stark Industries', 'first_name': 'Hart', 'last_name': 'Hagerty', 'email': 'hart@stark.example.com',
      'phone': '(555) 987-6543', 'instagram': '@starkind', 'category': 'Ceramics / Glass',
      'lead_source': 'Shopify lead form', 'tags': ['Late Pay']},
     ['applied', 'in_review', 'approved', 'suspended'], 'Interested'),
    ({'brand_name': 'Wayne Enterprises', 'first_name': 'Brice', 'last_name': 'Swyre', 'email': 'brice@wayne.example.com',
      'phone': '(555) 555-5555', 'instagram': '@wayne_ent', 'category': 'Vintage Clothing', 'lead_source': 'Email'},
     ['applied', 'in_review'], 'Interested'),
    ({'brand_name': 'Moss & Thread', 'instagram': '@mossandthread', 'lead_source': 'Instagram'}, [], None),
    ({'brand_name': 'Ember Wick Co.', 'first_name': 'Lillian', 'last_name': 'Park', 'email': 'lillian@emberwick.example.com',
      'phone': '(555) 210-3344', 'instagram': '@emberwickco', 'category': 'Soaps / Candles and Perfume',
      'lead_source': 'Application form'},
     ['applied'], 'Callback Requested'),
    ({'brand_name': 'Second Spin Records', 'first_name': 'Marcus', 'last_name': 'Bell', 'email': 'marcus@secondspin.example.com',
      'phone': '(555) 678-9012', 'instagram': '@secondspinrecords', 'category': 'Music / Vinyl',
      'lead_source': 'Shopify popup form'},
     ['applied', 'in_review', 'waitlisted'], 'Interested'),
    ({'brand_name': 'Fast Fashion Outlet', 'first_name': 'Dana', 'last_name': 'Ruiz', 'email': 'dana@ffoutlet.example.com',
      'phone': '(555) 432-1098', 'category': 'Clothing Brand', 'lead_source': 'Meta ads'},
     ['declined'], 'Not Qualified'),
]


class Command(BaseCommand):
    help = 'Create demo users and vendors for local development. Refuses to run with DEBUG off.'

    def add_arguments(self, parser):
        parser.add_argument('--password', help='Password for both demo users (default: random, printed once).')

    @transaction.atomic
    def handle(self, *args, password=None, **options):
        if not settings.DEBUG:
            raise CommandError('seed_dev only runs with DJANGO_DEBUG=true.')
        if Vendor.objects.exists():
            self.stdout.write(self.style.WARNING('Vendors already exist; nothing seeded.'))
            return

        admin_password = password or secrets.token_urlsafe(12)
        team_password = password or secrets.token_urlsafe(12)
        admin = self._user('admin', 'Avery', 'Admin', User.Role.ADMINISTRATOR, admin_password, superuser=True)
        member = self._user('wambui', 'Wambui', 'Kariuki', User.Role.TEAM_MEMBER, team_password)

        for fields, path, outcome in DEMO_VENDORS:
            vendor = services.register_lead(actor=member, data=fields, owner=member)
            call = vendor.tasks.filter(task_type=Task.TaskType.DISCOVERY_CALL).first()
            if outcome and call:
                callback = timezone.now() + timedelta(days=1) if outcome == 'Callback Requested' else None
                log_call(call, member, outcome, notes='Seeded call.', callback_at=callback,
                         call_details={'interest_level': 4 if outcome == 'Interested' else 2})
            for status in path:
                actor = member if status == 'applied' else admin
                services.change_vendor_status(vendor, status, actor=actor, note='Seed data')
                vendor.refresh_from_db()

        self.stdout.write(self.style.SUCCESS(f'Seeded {len(DEMO_VENDORS)} vendors.'))
        self.stdout.write(f'  admin  / {admin_password}  (Administrator, also Django admin superuser)')
        self.stdout.write(f'  wambui / {team_password}  (Team Member)')

    def _user(self, username, first, last, role, password, superuser=False):
        user, _ = User.objects.get_or_create(
            username=username, defaults={'first_name': first, 'last_name': last, 'role': role, 'email': f'{username}@example.com'},
        )
        user.role = role
        user.is_staff = superuser
        user.is_superuser = superuser
        user.set_password(password)
        user.save()
        return user
