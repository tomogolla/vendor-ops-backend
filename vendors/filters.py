import django_filters
from django.db.models import Exists, OuterRef, Q

from tasks.models import Task

from .models import Application, ApplicationLine, Vendor

Stage = Vendor.JourneyStage
AppStatus = Application.Status
AppStage = Application.Stage

# DEPRECATED (v1.3): mirrors Vendor.display_status. 'waitlisted' no longer exists as a vendor
# status (waitlisting is per line) and matches nothing.
DISPLAY_STATUS_QUERIES = {
    'lead': Q(journey_stage=Stage.LEAD) & ~Q(qualification_status=Vendor.Qualification.NOT_QUALIFIED),
    'applied': Q(journey_stage=Stage.APPLICANT, application_status_summary=AppStatus.SUBMITTED),
    'in_review': Q(
        journey_stage=Stage.APPLICANT, application_status_summary__in=[AppStatus.IN_REVIEW, AppStatus.WAITLISTED],
    ),
    'waitlisted': Q(pk__in=[]),
    'approved': Q(journey_stage=Stage.APPROVED)
    | Q(journey_stage=Stage.APPLICANT, application_status_summary=AppStatus.APPROVED),
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
    label = django_filters.CharFilter(method='filter_label', help_text='Comma-separated: prospect, active, inactive, suspended.')
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

    def filter_label(self, queryset, name, value):
        labels = [part.strip() for part in value.split(',') if part.strip()]
        if 'label_value' not in queryset.query.annotations:
            queryset = queryset.with_label()
        return queryset.filter(label_value__in=labels)

    def filter_call(self, queryset, name, value):
        calls = Task.objects.filter(vendor=OuterRef('pk'), task_type=Task.TaskType.DISCOVERY_CALL)
        if value == 'due':
            return queryset.filter(Exists(calls.filter(status__in=Task.OPEN_STATUSES)))
        return queryset.exclude(Exists(calls.filter(attempt_count__gt=0)))


# A call outcome that means somebody actually spoke to the vendor.
REACHED_OUTCOMES = [Task.CallOutcome.INTERESTED, Task.CallOutcome.NOT_QUALIFIED, Task.CallOutcome.CALLBACK_REQUESTED]


class ApplicationFilter(django_filters.FilterSet):
    stage = django_filters.CharFilter(method='filter_stage', help_text='Comma-separated stages.')
    has_waitlisted_lines = django_filters.BooleanFilter(method='filter_waitlisted')
    call = django_filters.ChoiceFilter(method='filter_call', choices=[
        ('none', 'New, no call attempt logged'),
        ('attempted_not_reached', 'Call attempted, vendor not reached yet'),
    ])
    owner = django_filters.CharFilter(method='filter_owner', help_text='Vendor owner: user id, "me" or "none".')
    source = django_filters.CharFilter(field_name='source', lookup_expr='iexact')
    weekend = django_filters.UUIDFilter(method='filter_weekend', help_text='Applications with a line on this weekend.')
    reviewer = django_filters.CharFilter(method='filter_reviewer')
    submitted_after = django_filters.IsoDateTimeFilter(field_name='submitted_at', lookup_expr='gte')
    submitted_before = django_filters.IsoDateTimeFilter(field_name='submitted_at', lookup_expr='lt')

    class Meta:
        model = Application
        fields = ['vendor', 'status']  # `status` is DEPRECATED (v1.3); filter on `stage`.

    def filter_stage(self, queryset, name, value):
        return queryset.filter(stage__in=[part.strip() for part in value.split(',') if part.strip()])

    def filter_waitlisted(self, queryset, name, value):
        waitlisted = Exists(ApplicationLine.objects.filter(
            application=OuterRef('pk'), status=ApplicationLine.Status.WAITLISTED,
        ))
        return queryset.filter(waitlisted) if value else queryset.exclude(waitlisted)

    def filter_call(self, queryset, name, value):
        calls = Task.objects.filter(application=OuterRef('pk'), task_type=Task.TaskType.DISCOVERY_CALL)
        attempted = Exists(calls.filter(attempt_count__gt=0))
        if value == 'none':
            return queryset.filter(stage=AppStage.NEW).exclude(attempted)
        return queryset.filter(attempted).exclude(Exists(calls.filter(outcome__in=REACHED_OUTCOMES)))

    def filter_owner(self, queryset, name, value):
        return filter_owner(queryset, 'vendor__owner', value, self.request)

    def filter_weekend(self, queryset, name, value):
        on_weekend = ApplicationLine.objects.filter(
            Q(variant__weekend=value) | Q(add_on__weekend=value), application=OuterRef('pk'),
        )
        return queryset.filter(Exists(on_weekend))

    def filter_reviewer(self, queryset, name, value):
        return filter_owner(queryset, 'reviewer', value, self.request)
