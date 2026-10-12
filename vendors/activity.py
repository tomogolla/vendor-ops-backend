from .models import ActivityEvent


def record_activity(vendor, event_type, description, actor=None, application=None, task=None, **metadata):
    return ActivityEvent.objects.create(
        vendor=vendor,
        application=application,
        task=task,
        event_type=event_type,
        description=description[:500],
        actor=actor,
        metadata=metadata,
    )
