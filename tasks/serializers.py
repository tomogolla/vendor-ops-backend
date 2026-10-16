from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSummarySerializer
from vendors.models import Application, Vendor
from vendors.serializers import VendorSummarySerializer

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    vendor = serializers.PrimaryKeyRelatedField(queryset=Vendor.objects.all())
    vendor_detail = VendorSummarySerializer(source='vendor', read_only=True)
    application = serializers.PrimaryKeyRelatedField(
        queryset=Application.objects.all(), allow_null=True, required=False
    )
    owner = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True), allow_null=True, required=False
    )
    owner_detail = UserSummarySerializer(source='owner', read_only=True)
    created_by = UserSummarySerializer(read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = [
            'id', 'vendor', 'vendor_detail', 'application', 'title', 'task_type', 'status', 'priority',
            'owner', 'owner_detail', 'created_by', 'due_at', 'completed_at', 'description', 'outcome', 'notes',
            'attempt_count', 'last_attempt_at', 'callback_at', 'call_details', 'is_open', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'created_by', 'completed_at', 'attempt_count', 'last_attempt_at', 'is_open',
            'created_at', 'updated_at',
        ]
        # The partial unique constraints are checked in validate() with clearer messages.
        validators = []

    def validate(self, attrs):
        user = self.context['request'].user
        admin = getattr(user, 'is_administrator', False)
        if self.instance is not None:
            if 'vendor' in attrs and attrs['vendor'] != self.instance.vendor:
                raise serializers.ValidationError({'vendor': 'A task cannot move to another vendor.'})
            if 'task_type' in attrs and attrs['task_type'] != self.instance.task_type:
                raise serializers.ValidationError({'task_type': 'Task type cannot be changed.'})
            if not admin and 'owner' in attrs and attrs['owner'] != self.instance.owner:
                claiming = self.instance.owner_id is None and attrs['owner'] == user
                if not claiming:
                    raise serializers.ValidationError({'owner': 'You can only claim unassigned tasks.'})
        elif not admin and attrs.get('owner') not in (None, user):
            raise serializers.ValidationError({'owner': 'Only administrators can assign tasks to other people.'})

        application = attrs.get('application')
        vendor = attrs.get('vendor', getattr(self.instance, 'vendor', None))
        if application and vendor and application.vendor_id != vendor.pk:
            raise serializers.ValidationError({'application': 'That application belongs to another vendor.'})

        task_type = attrs.get('task_type', getattr(self.instance, 'task_type', None))
        status = attrs.get('status', getattr(self.instance, 'status', Task.Status.TODO))
        if task_type == Task.TaskType.EMAIL_REVIEW:
            # Review email tasks belong to their draft: they are created with it and closed by
            # approving or cancelling it, never by a normal task update.
            if self.instance is None:
                raise serializers.ValidationError({'task_type': 'Review email tasks are created with an email draft.'})
            closing = 'status' in attrs and status not in Task.OPEN_STATUSES
            if closing and self.instance.email_messages.filter(status='draft').exists():
                raise serializers.ValidationError(
                    {'status': 'Approve or cancel the email draft instead; that closes this task.'}
                )
        if status in Task.OPEN_STATUSES and task_type in (Task.TaskType.DISCOVERY_CALL, Task.TaskType.APPLICATION_REVIEW):
            lookup = {'vendor': vendor} if task_type == Task.TaskType.DISCOVERY_CALL else {'application': application}
            if task_type == Task.TaskType.APPLICATION_REVIEW and not application:
                raise serializers.ValidationError({'application': 'Review tasks need an application.'})
            clash = Task.objects.filter(task_type=task_type, status__in=Task.OPEN_STATUSES, **lookup)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({'task_type': 'An open task of this type already exists.'})
        return attrs

    def update(self, instance, validated_data):
        new_status = validated_data.get('status')
        if new_status == Task.Status.COMPLETED and instance.status != Task.Status.COMPLETED:
            validated_data['completed_at'] = timezone.now()
        elif new_status and new_status in Task.OPEN_STATUSES:
            validated_data['completed_at'] = None
        return super().update(instance, validated_data)


class LogCallSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=Task.CallOutcome.choices)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    callback_at = serializers.DateTimeField(required=False, allow_null=True)
    call_details = serializers.DictField(required=False)

    def validate_call_details(self, value):
        interest = value.get('interest_level')
        if interest is not None and (not isinstance(interest, int) or not 0 <= interest <= 5):
            raise serializers.ValidationError('interest_level must be a whole number from 0 to 5.')
        allowed = {
            'interest_level', 'challenges', 'location_pain_points', 'revenue_consistency',
            'vendor_reaction', 'objection_notes',
        }
        unknown = set(value) - allowed
        if unknown:
            raise serializers.ValidationError(f'Unknown keys: {", ".join(sorted(unknown))}.')
        return {
            key: (val.strip() if isinstance(val, str) else val) for key, val in value.items()
        }
