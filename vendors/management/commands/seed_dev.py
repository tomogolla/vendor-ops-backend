import secrets
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import AddOn, BoothVariant, MarketWeekend
from tasks.models import Task
from tasks.services import log_call
from vendors import pipeline, services
from vendors.models import Application, Vendor

Stage = Application.Stage

DEMO_VENDORS = [
    # (vendor fields, call outcome or None, target stage, booth lines [(weekend key, variant name, quantity, add-ons)])
    ({'brand_name': 'Acme Corporation', 'first_name': 'Cy', 'last_name': 'Ganderton', 'email': 'cy@acme.example.com',
      'phone': '(555) 123-4567', 'instagram': '@acme', 'website': 'https://acme.example.com',
      'category': 'Arts, Prints & Stickers', 'lead_source': 'Meta ads', 'tags': ['VIP', 'Tier-1'],
      'description': 'Independent artwork, prints, and stickers.', 'commitment_level': 'Full time'},
     'Interested', Stage.WON, [('oct10', '8ft x 8ft', 2, ['Tent rental'])]),
    ({'brand_name': 'Stark Industries', 'first_name': 'Hart', 'last_name': 'Hagerty', 'email': 'hart@stark.example.com',
      'phone': '(555) 987-6543', 'instagram': '@starkind', 'category': 'Ceramics / Glass',
      'lead_source': 'Shopify lead form', 'tags': ['Late Pay']},
     'Interested', 'suspended', [('oct17', '8ft x 8ft', 1, ['Table'])]),
    ({'brand_name': 'Wayne Enterprises', 'first_name': 'Brice', 'last_name': 'Swyre', 'email': 'brice@wayne.example.com',
      'phone': '(555) 555-5555', 'instagram': '@wayne_ent', 'category': 'Vintage Clothing', 'lead_source': 'Email'},
     'Interested', Stage.AWAITING_PAYMENT, [('oct10', '10ft x 10ft', 1, [])]),
    ({'brand_name': 'Moss & Thread', 'instagram': '@mossandthread', 'lead_source': 'Instagram'},
     None, Stage.NEW, [('oct17', '8ft x 8ft', 1, [])]),
    ({'brand_name': 'Ember Wick Co.', 'first_name': 'Lillian', 'last_name': 'Park', 'email': 'lillian@emberwick.example.com',
      'phone': '(555) 210-3344', 'instagram': '@emberwickco', 'category': 'Soaps / Candles and Perfume',
      'lead_source': 'Application form'},
     'Callback Requested', Stage.IN_REVIEW, [('oct17', '8ft x 8ft', 1, ['Table'])]),
    ({'brand_name': 'Second Spin Records', 'first_name': 'Marcus', 'last_name': 'Bell', 'email': 'marcus@secondspin.example.com',
      'phone': '(555) 678-9012', 'instagram': '@secondspinrecords', 'category': 'Music / Vinyl',
      'lead_source': 'Shopify popup form'},
     'Interested', 'waitlisted', [('oct10', '8ft x 8ft', 1, [])]),
    ({'brand_name': 'Fast Fashion Outlet', 'first_name': 'Dana', 'last_name': 'Ruiz', 'email': 'dana@ffoutlet.example.com',
      'phone': '(555) 432-1098', 'category': 'Clothing Brand', 'lead_source': 'Meta ads'},
     'Not Qualified', Stage.DECLINED, [('oct10', '8ft x 8ft', 1, [])]),
    ({'brand_name': 'Opal & Fern', 'first_name': 'Rosa', 'last_name': 'Quinn', 'email': 'rosa@opalfern.example.com',
      'instagram': '@opalandfern', 'category': 'Jewelry', 'lead_source': 'Instagram'},
     'Interested', Stage.WITHDRAWN, [('oct17', '8ft x 8ft', 1, [])]),
]


class Command(BaseCommand):
    help = 'Create demo users, market weekends and vendors for local development. Refuses to run with DEBUG off.'

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
        products = self._catalog()

        for fields, outcome, target, lines in DEMO_VENDORS:
            vendor = services.register_lead(actor=member, data=fields, owner=member)
            application = vendor.applications.get()
            call = application.tasks.filter(task_type=Task.TaskType.DISCOVERY_CALL).first()
            if outcome and call:
                callback = timezone.now() + timedelta(days=1) if outcome == 'Callback Requested' else None
                log_call(call, member, outcome, notes='Seeded call.', callback_at=callback,
                         call_details={'interest_level': 4 if outcome == 'Interested' else 2})
            booths = []
            for weekend, variant, quantity, add_ons in lines:
                booth = pipeline.add_line(application, actor=member, variant=products[weekend, variant], quantity=quantity)
                extras = [pipeline.add_line(application, actor=member, add_on=products[weekend, name], parent_line=booth)
                          for name in add_ons]
                booths.append((booth, extras))
            self._advance(application, target, booths, admin)

        self.stdout.write(self.style.SUCCESS(f'Seeded 2 weekends and {len(DEMO_VENDORS)} vendors.'))
        self.stdout.write(f'  admin  / {admin_password}  (Administrator, also Django admin superuser)')
        self.stdout.write(f'  wambui / {team_password}  (Team Member)')

    def _advance(self, application, target, booths, admin):
        application.refresh_from_db()
        if target == 'waitlisted':
            # Oct 10 & 11 8ft x 8ft is already full (Acme holds both spots).
            for booth, _ in booths:
                pipeline.waitlist_line(booth, actor=admin, reason='Weekend full')
            return
        if target in (Stage.DECLINED, Stage.WITHDRAWN):
            pipeline.change_application_stage(application, target, admin, reason='Seed data')
            return
        if target in (Stage.WON, Stage.AWAITING_PAYMENT, 'suspended'):
            for booth, extras in booths:
                pipeline.confirm_line(booth, actor=admin)
                for extra in extras:
                    pipeline.confirm_line(extra, actor=admin)
            if application.vendor.brand_name == 'Acme Corporation':
                pipeline.set_discount(application, actor=admin, discount_type=Application.DiscountType.PERCENT,
                                      value='10', label='New Vendor Trial')
            pipeline.mark_billed(application, actor=admin)
            if target != Stage.AWAITING_PAYMENT:
                pipeline.mark_first_payment(application, actor=admin, note='Seed data')
            if target == 'suspended':
                vendor = Vendor.objects.get(pk=application.vendor_id)
                services.change_vendor_status(vendor, 'suspended', actor=admin, note='Seed data')

    def _catalog(self) -> dict:
        year = date.today().year
        oct10 = MarketWeekend.objects.create(label='Oct 10 & 11', start_date=date(year, 10, 10),
                                             end_date=date(year, 10, 11), venue='Main lot', status=MarketWeekend.Status.OPEN)
        oct17 = MarketWeekend.objects.create(label='Oct 17 & 18', start_date=date(year, 10, 17),
                                             end_date=date(year, 10, 18), venue='Main lot', status=MarketWeekend.Status.OPEN)
        products = {}
        for key, weekend, small_capacity in (('oct10', oct10, 2), ('oct17', oct17, 12)):
            products[key, '8ft x 8ft'] = BoothVariant.objects.create(
                weekend=weekend, name='8ft x 8ft', dimensions='8 x 8 ft', price_cents=15000, capacity=small_capacity)
            products[key, '10ft x 10ft'] = BoothVariant.objects.create(
                weekend=weekend, name='10ft x 10ft', dimensions='10 x 10 ft', price_cents=22500, capacity=4)
        products['oct10', 'Tent rental'] = AddOn.objects.create(weekend=oct10, name='Tent rental', price_cents=4000, capacity=3)
        products['oct17', 'Table'] = AddOn.objects.create(weekend=oct17, name='Table', price_cents=1000)
        return products

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
