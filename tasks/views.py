import django_filters
from django.db.models import Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounts.permissions import is_admin
from vendors.activity import record_activity
from vendors.filters import filter_owner
from vendors.models import ActivityEvent

from . import services
from .models import Task
from .serializers import LogCallSerializer, TaskSerializer


class TaskFilter(django_filters.FilterSet):
    owner = django_filters.CharFilter(method='filter_owner', help_text='User id, "me" or "none".')
    status = django_filters.CharFilter(method='filter_status', help_text='Comma-separated statuses.')
    is_open = django_filters.BooleanFilter(method='filter_open')
    due_before = django_filters.IsoDateTimeFilter(field_name='due_at', lookup_expr='lt')
    created_after = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')

    class Meta:
        model = Task
        fields = ['task_type', 'vendor', 'application', 'priority']

    def filter_owner(self, queryset, name, value):
        return filter_owner(queryset, 'owner', value, self.request)

    def filter_status(self, queryset, name, value):
        return queryset.filter(status__in=[part.strip() for part in value.split(',') if part.strip()])

    def filter_open(self, queryset, name, value):
        open_q = Q(status__in=Task.OPEN_STATUSES)
        return queryset.filter(open_q) if value else queryset.exclude(open_q)


class TaskViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    serializer_class = TaskSerializer
    filterset_class = TaskFilter
    ordering_fields = ['due_at', 'created_at', 'priority', 'updated_at']
    search_fields = ['title', 'vendor__brand_name']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        tasks = Task.objects.select_related('vendor', 'owner', 'created_by', 'application')
        user = self.request.user
        if is_admin(user):
            return tasks
        # Team members see their own tasks plus the unassigned team queue.
        return tasks.filter(Q(owner=user) | Q(owner__isnull=True))

    def _require_actionable(self, task: Task):
        user = self.request.user
        if not is_admin(user) and task.owner_id not in (None, user.pk):
            raise PermissionDenied('You can only work on tasks assigned to you.')

    def perform_create(self, serializer):
        task = serializer.save(created_by=self.request.user)
        record_activity(
            task.vendor, ActivityEvent.EventType.TASK_CREATED,
            f'{task.get_task_type_display()} task created: {task.title}',
            actor=self.request.user, application=task.application, task=task, task_type=task.task_type,
        )

    def perform_update(self, serializer):
        self._require_actionable(serializer.instance)
        serializer.save()

    @action(detail=True, methods=['post'], url_path='log-call')
    def log_call(self, request, pk=None):
        task = self.get_object()
        self._require_actionable(task)
        serializer = LogCallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.log_call(task, request.user, **serializer.validated_data)
        task.refresh_from_db()
        return Response(TaskSerializer(task, context=self.get_serializer_context()).data, status=status.HTTP_200_OK)
