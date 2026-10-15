"""Seeds the email template catalog. Every template starts Off, so this sends nothing.

Copy is placeholder text for the team to rewrite in the admin before any template is moved to
Draft. Existing templates (same key) are left untouched, so re-running is safe. Reversing removes
only seeded templates that have never been used.
"""

from django.db import migrations

SIGN_OFF_TEXT = '\n\n{owner_name}\nThe Good Flea'
SIGN_OFF_HTML = '<p>{owner_name}<br>The Good Flea</p>'

TEMPLATES = [
    {
        'key': 'application_received', 'name': 'Application received', 'category': 'transactional',
        'description': 'Confirms a new application (reference included).',
        'allowed_variables': ['vendor_name', 'application_reference', 'owner_name'],
        'subject_template': 'We received your application, {vendor_name}',
        'text_template': 'Hi {vendor_name},\n\nThanks for applying to The Good Flea. Your reference is '
                         '{application_reference}. We will call you shortly to talk about your booth.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>Thanks for applying to The Good Flea. Your reference is '
                         '<strong>{application_reference}</strong>. We will call you shortly to talk about your booth.</p>'
                         + SIGN_OFF_HTML,
    },
    {
        'key': 'call_follow_up', 'name': 'Call follow-up', 'category': 'operational',
        'description': 'Sent after a discovery call attempt.',
        'allowed_variables': ['vendor_name', 'owner_name'],
        'subject_template': 'Following up on our call, {vendor_name}',
        'text_template': 'Hi {vendor_name},\n\nWe tried to reach you about your application to The Good Flea. '
                         'Reply to this email with a good time to talk.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>We tried to reach you about your application to The Good Flea. '
                         'Reply to this email with a good time to talk.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'waitlist_update', 'name': 'Waitlist update', 'category': 'transactional',
        'description': 'A requested booth is waitlisted for a weekend.',
        'allowed_variables': ['vendor_name', 'application_reference', 'market_name', 'market_dates', 'owner_name'],
        'subject_template': 'You are on the waitlist for {market_name}',
        'text_template': 'Hi {vendor_name},\n\n{market_name} ({market_dates}) is full for the booth you asked for, '
                         'so we have added you to the waitlist (reference {application_reference}). We will let you '
                         'know if a spot opens up.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>{market_name} ({market_dates}) is full for the booth you asked for, '
                         'so we have added you to the waitlist (reference {application_reference}). We will let you '
                         'know if a spot opens up.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'application_declined', 'name': 'Application declined', 'category': 'transactional',
        'description': 'The application was declined.',
        'allowed_variables': ['vendor_name', 'application_reference', 'owner_name'],
        'subject_template': 'Your application to The Good Flea',
        'text_template': 'Hi {vendor_name},\n\nThank you for applying (reference {application_reference}). '
                         'We are not able to offer you a booth this time.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>Thank you for applying (reference {application_reference}). '
                         'We are not able to offer you a booth this time.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'market_preparation', 'name': 'Market preparation', 'category': 'operational',
        'description': 'Reminder before a market weekend the vendor is booked for.',
        'allowed_variables': ['vendor_name', 'market_name', 'market_dates', 'owner_name'],
        'subject_template': 'Getting ready for {market_name}',
        'text_template': 'Hi {vendor_name},\n\n{market_name} is coming up on {market_dates}. Here is what to '
                         'prepare before you arrive.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>{market_name} is coming up on {market_dates}. Here is what to '
                         'prepare before you arrive.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'market_information_packet', 'name': 'Market information packet', 'category': 'operational',
        'description': 'Load-in times, location and rules for a market weekend.',
        'allowed_variables': ['vendor_name', 'market_name', 'market_dates', 'owner_name'],
        'subject_template': 'Your information packet for {market_name}',
        'text_template': 'Hi {vendor_name},\n\nHere is the information packet for {market_name} ({market_dates}): '
                         'load-in times, location and market rules.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>Here is the information packet for {market_name} '
                         '({market_dates}): load-in times, location and market rules.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'post_market_follow_up', 'name': 'Post-market follow-up', 'category': 'operational',
        'description': 'Thanks and feedback request after a market weekend.',
        'allowed_variables': ['vendor_name', 'market_name', 'owner_name'],
        'subject_template': 'Thanks for joining {market_name}',
        'text_template': 'Hi {vendor_name},\n\nThank you for being part of {market_name}. We would love to hear '
                         'how it went for you.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>Thank you for being part of {market_name}. We would love to hear '
                         'how it went for you.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'invoice_sent', 'name': 'Invoice sent', 'category': 'transactional',
        'description': 'Catalog entry only: wired when invoicing exists. Not used by the interim billing action.',
        'allowed_variables': ['vendor_name', 'application_reference', 'payment_link', 'owner_name'],
        'subject_template': 'Your invoice from The Good Flea ({application_reference})',
        'text_template': 'Hi {vendor_name},\n\nYour invoice for {application_reference} is ready. Pay here: '
                         '{payment_link}' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>Your invoice for {application_reference} is ready. '
                         '<a href="{payment_link}">Pay your invoice</a>.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'payment_reminder', 'name': 'Payment reminder', 'category': 'transactional',
        'description': 'Catalog entry only: wired when invoicing exists. Needs an "unpaid" stop condition.',
        'allowed_variables': ['vendor_name', 'application_reference', 'payment_link', 'owner_name'],
        'subject_template': 'Reminder: your invoice for {application_reference}',
        'text_template': 'Hi {vendor_name},\n\nA reminder that your invoice for {application_reference} is still '
                         'open. Pay here: {payment_link}' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>A reminder that your invoice for {application_reference} is '
                         'still open. <a href="{payment_link}">Pay your invoice</a>.</p>' + SIGN_OFF_HTML,
    },
    {
        'key': 'payment_received', 'name': 'Payment received', 'category': 'transactional',
        'description': 'Catalog entry only: wired when payments are recorded from the payment provider.',
        'allowed_variables': ['vendor_name', 'application_reference', 'owner_name'],
        'subject_template': 'Payment received, thank you ({application_reference})',
        'text_template': 'Hi {vendor_name},\n\nWe received your payment for {application_reference}. '
                         'Your booth is booked.' + SIGN_OFF_TEXT,
        'html_template': '<p>Hi {vendor_name},</p><p>We received your payment for {application_reference}. '
                         'Your booth is booked.</p>' + SIGN_OFF_HTML,
    },
]


def seed(apps, schema_editor):
    EmailTemplate = apps.get_model('communications', 'EmailTemplate')
    for template in TEMPLATES:
        EmailTemplate.objects.get_or_create(key=template['key'], defaults={**template, 'mode': 'off', 'is_active': True})


def unseed(apps, schema_editor):
    EmailTemplate = apps.get_model('communications', 'EmailTemplate')
    EmailTemplate.objects.filter(key__in=[t['key'] for t in TEMPLATES], messages__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
