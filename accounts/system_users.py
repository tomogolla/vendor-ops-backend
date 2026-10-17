"""System accounts: recorded as the actor for work no person did (e.g. a website submission).

They are inactive and have no usable password, so they can never sign in, and `is_system` keeps
them out of the team list, assignee pickers and the user admin."""

from .models import User

WEBSITE_FORM_USERNAME = 'website-form'
WEBSITE_FORM_NAME = 'Website form'


def website_form_user() -> User:
    """The actor for public application submissions; created on first use."""
    user, created = User.objects.get_or_create(
        username=WEBSITE_FORM_USERNAME,
        defaults={'first_name': WEBSITE_FORM_NAME, 'is_active': False, 'is_system': True, 'role': User.Role.TEAM_MEMBER},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    return user
