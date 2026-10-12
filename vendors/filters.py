import django_filters
from django.db.models import Exists, OuterRef, Q

from tasks.models import Task

from .models import Application, Vendor

Stage = Vendor.JourneyStage
AppStatus = Application.Status

DISPLAY_STATUS_QUERIES = {
    'lead': Q(journey_stage=Stage.LEAD) & ~Q(qualification_status=Vendor.Qualification.NOT_QUALIFIED),
    'applied': Q(journey_stage=Stage.APPLICANT, application_status_summary=AppStatus.SUBMITTED),
    'in_review': Q(journey_stage=Stage.APPLICANT, application_status_summary=AppStatus.IN_REVIEW),
    'waitlisted': Q(journey_stage=Stage.APPLICANT, application_status_summary=AppStatus.WAITLISTED),
    'approved': Q(journey_stage=Stage.APPROVED),
    'suspended': Q(journey_stage=Stage.SUSPENDED),
    'declined': Q(journey_stage=Stage.LEAD, qualification_status=Vendor.Qualification.NOT_QUALIFIED)
    | Q(journey_stage=Stage.APPLICANT, application_status_summary__in=[AppStatus.DECLINED, AppStatus.WITHDRAWN]),
}


def filter_owner(queryset, field, value, request):
    if value == 'me':
        return queryset.filter(**{field: request.user})
    if value == 'none':
        return queryset.filter(**{f'{field}__isnull': True})
    return queryset.filter(**{field: value})


class VendorFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(method='filter_status', help_text='Comma-separated display statuses.')
    application_status = django_filters.CharFilter(field_name='application_status_summary')
    lead_source = django_filters.CharFilter(field_name='lead_source', lookup_expr='iexact')
    owner = django_filters.CharFilter(method='filter_owner', help_text='User id, "me" or "none".')
    call = django_filters.ChoiceFilter(
        method='filter_call', choices=[('due', 'Open discovery call'), ('none', 'Never called')]
    )
    created_after = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_before = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='lt')

    class Meta:
        model = Vendor
        fields = ['journey_stage', 'qualification_status', 'is_active', 'category']

    def filter_status(self, queryset, name, value):
        query = Q()
        for status in filter(None, (part.strip() for part in value.split(','))):
            if status not in DISPLAY_STATUS_QUERIES:
                return queryset.none()
            query |= DISPLAY_STATUS_QUERIES[status]
        return queryset.filter(query)

    def filter_owner(self, queryset, name, value):
        return filter_owner(queryset, 'owner', value, self.request)

    def filter_call(self, queryset, name, value):
        calls = Task.objects.filter(vendor=OuterRef('pk'), task_type=Task.TaskType.DISCOVERY_CALL)
        if value == 'due':
            return queryset.filter(Exists(calls.filter(status__in=Task.OPEN_STATUSES)))
        return queryset.exclude(Exists(calls.filter(attempt_count__gt=0)))


class ApplicationFilter(django_filters.FilterSet):
    reviewer = django_filters.CharFilter(method='filter_reviewer')
    submitted_after = django_filters.IsoDateTimeFilter(field_name='submitted_at', lookup_expr='gte')
    submitted_before = django_filters.IsoDateTimeFilter(field_name='submitted_at', lookup_expr='lt')

    class Meta:
        model = Application
        fields = ['vendor', 'status']

    def filter_reviewer(self, queryset, name, value):
        return filter_owner(queryset, 'reviewer', value, self.request)
