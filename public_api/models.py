from django.db import models


class GoogleSheetsSubmission(models.Model):
    """Idempotency receipt for an Apps Script form-submit event."""

    submission_id = models.CharField(max_length=500, unique=True)
    sheet_type = models.CharField(max_length=50)
    sheet_name = models.CharField(max_length=200, blank=True)
    row_number = models.PositiveIntegerField(null=True, blank=True)
    raw_fields = models.JSONField(default=dict, blank=True)
    application = models.ForeignKey('vendors.Application', on_delete=models.PROTECT, related_name='+')
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
