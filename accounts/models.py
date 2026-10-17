import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMINISTRATOR = 'ADMINISTRATOR', 'Administrator'
        TEAM_MEMBER = 'TEAM_MEMBER', 'Team Member'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.TEAM_MEMBER)
    # System accounts (e.g. "Website form") are inactive, have no usable password and are hidden from
    # the team list, assignee pickers and the user admin by default.
    is_system = models.BooleanField(default=False, db_default=False, help_text='A non-person account used by the system.')

    class Meta:
        ordering = ['first_name', 'last_name', 'username']

    @property
    def is_administrator(self) -> bool:
        return self.is_active and (self.role == self.Role.ADMINISTRATOR or self.is_superuser)

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username
