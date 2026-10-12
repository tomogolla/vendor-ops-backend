import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Task(models.Model):
    class TaskType(models.TextChoices):
        DISCOVERY_CALL = 'DISCOVERY_CALL', 'Discovery call'
        APPLICATION_REVIEW = 'APPLICATION_REVIEW', 'Application review'
        FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'

    class Status(models.TextChoices):
        TODO = 'TODO', 'To do'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        COMPLETED = 'COMPLETED', 'Completed'
        NO_ANSWER = 'NO_ANSWER', 'No answer'
        RESCHEDULED = 'RESCHEDULED', 'Rescheduled'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class Priority(models.TextChoices):
        LOW = 'LOW', 'Low'
        NORMAL = 'NORMAL', 'Normal'
        HIGH = 'HIGH', 'High'
        URGENT = 'URGENT', 'Urgent'

    class CallOutcome(models.TextChoices):
        INTERESTED = 'Interested', 'Interested'
        NOT_QUALIFIED = 'Not Qualified', 'Not qualified'
        CALLBACK_REQUESTED = 'Callback Requested', 'Callback requested'
        NO_ANSWER = 'No Answer', 'No answer'
        WRONG_NUMBER = 'Wrong Number', 'Wrong number'

    OPEN_STATUSES = (Status.TODO, Status.IN_PROGRESS, Status.NO_ANSWER, Status.RESCHEDULED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey('vendors.Vendor', on_delete=models.CASCADE, related_name='tasks')
    application = models.ForeignKey(
        'vendors.Application', null=True, blank=True, on_delete=models.CASCADE, related_name='tasks'
    )
    title = models.CharField(max_length=200)
    task_type = models.CharField(max_length=30, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks',
        help_text='Empty means the task sits in the team queue.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Null when created by the system.',
    )
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    outcome = models.CharField(max_length=40, blank=True)
    notes = models.TextField(blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    callback_at = models.DateTimeField(null=True, blank=True)
    call_details = models.JSONField(
        default=dict, blank=True, help_text='Interest level and call-script answers from the latest call.'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_at', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['vendor'],
                condition=Q(task_type='DISCOVERY_CALL', status__in=['TODO', 'IN_PROGRESS', 'NO_ANSWER', 'RESCHEDULED']),
                name='one_open_discovery_call_per_vendor',
            ),
            models.UniqueConstraint(
                fields=['application'],
                condition=Q(task_type='APPLICATION_REVIEW', status__in=['TODO', 'IN_PROGRESS', 'NO_ANSWER', 'RESCHEDULED']),
                name='one_open_review_per_application',
            ),
        ]
        indexes = [models.Index(fields=['task_type', 'status']), models.Index(fields=['owner', 'status'])]

    def __str__(self):
        return self.title

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES
