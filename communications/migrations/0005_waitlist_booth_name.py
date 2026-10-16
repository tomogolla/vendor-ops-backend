"""Lets the waitlist email say which booth it is about ({booth_name}).

- Adds `booth_name` to the waitlist_update template's allowed variables (additive).
- Rewrites its copy to mention the booth ONLY if the copy is still exactly the seeded original, so
  templates the team has already customised are never overwritten.
- Never changes any template's mode (they stay Off unless an administrator changed them).
"""

import importlib

from django.db import migrations

KEY = 'waitlist_update'
SEED = next(t for t in importlib.import_module('communications.migrations.0002_seed_templates').TEMPLATES if t['key'] == KEY)
SIGN_OFF_TEXT = '\n\n{owner_name}\nThe Good Flea'
SIGN_OFF_HTML = '<p>{owner_name}<br>The Good Flea</p>'
NEW_COPY = {
    'subject_template': 'Waitlist update for {market_name}: {booth_name}',
    'text_template': 'Hi {vendor_name},\n\nThe {booth_name} booth you asked for at {market_name} ({market_dates}) is full, '
                     'so we have added that booth to the waitlist (reference {application_reference}). Any other booths '
                     'you requested are not affected. We will let you know if a spot opens up.' + SIGN_OFF_TEXT,
    'html_template': '<p>Hi {vendor_name},</p><p>The <strong>{booth_name}</strong> booth you asked for at {market_name} '
                     '({market_dates}) is full, so we have added that booth to the waitlist (reference '
                     '{application_reference}). Any other booths you requested are not affected. We will let you know if '
                     'a spot opens up.</p>' + SIGN_OFF_HTML,
}
PARTS = ('subject_template', 'text_template', 'html_template')


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model('communications', 'EmailTemplate')
    template = EmailTemplate.objects.filter(key=KEY).first()
    if template is None:
        return
    changed = []
    if 'booth_name' not in template.allowed_variables:
        template.allowed_variables = [*template.allowed_variables, 'booth_name']
        changed.append('allowed_variables')
    if all(getattr(template, part) == SEED[part] for part in PARTS):
        for part in PARTS:
            setattr(template, part, NEW_COPY[part])
        changed.extend(PARTS)
    if changed:
        template.save(update_fields=[*changed, 'updated_at'])


def backwards(apps, schema_editor):
    EmailTemplate = apps.get_model('communications', 'EmailTemplate')
    template = EmailTemplate.objects.filter(key=KEY).first()
    if template is None:
        return
    if all(getattr(template, part) == NEW_COPY[part] for part in PARTS):
        for part in PARTS:
            setattr(template, part, SEED[part])
        template.allowed_variables = [name for name in template.allowed_variables if name != 'booth_name']
        template.save(update_fields=[*PARTS, 'allowed_variables', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('communications', '0004_message_context_and_draft_revisions'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
