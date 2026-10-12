import uuid

from django.conf import settings
from django.db import models


class ImportBatch(models.Model):
    """One confirmed CSV import. Previews are not stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file_name = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'import batches'

    def __str__(self):
        return f'{self.file_name or "CSV import"} ({self.created_at:%Y-%m-%d %H:%M})'
