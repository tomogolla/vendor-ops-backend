"""Email API (Slice 2) under /api/v1/email/. Business rules stay in services.py.

Who can do what:
- every signed-in team member can read settings, templates, messages and the overview, preview
  templates, prepare drafts and edit drafts;
- administrators, the person a review task is assigned to, and the application's reviewer can
  approve and cancel;
- only administrators change settings and templates.
"""

from django.db.models import Count, Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

import django_filters

from accounts.permissions import IsAdministrator, IsAdministratorOrReadOnly
from vendors.models import ActivityEvent, Application

from . import services
from .models import EmailMessage, EmailSettings, EmailTemplate
from .serializers import (
    CancelSerializer,
    EmailMessageDetailSerializer,
    EmailMessageSerializer,
    EmailRevisionSerializer,
    EmailSettingsSerializer,
    EmailTemplateSerializer,
    ReviseDraftSerializer,
    TemplatePreviewSerializer,
)

Status = EmailMessage.Status
MESSAGES = EmailMessage.objects.select_related(
    'template', 'vendor', 'application', 'market_weekend', 'application_line__variant__weekend',
    'application_line__add_on__weekend', 'review_task__owner', 'created_by',
)
# Overview buckets: every message is in exactly one bucket, by its current status.
BUCKETS = {
    'awaiting_review': [Status.DRAFT],
    'queued': [Status.QUEUED, Status.SENDING],
    'sent': [Status.SENT, Status.DELIVERED],
    'problems': [Status.FAILED, Status.BOUNCED, Status.COMPLAINED],
    'not_sent': [Status.SKIPPED, Status.CANCELLED],
}


class EmailSettingsView(APIView):
    """GET for everyone signed in (no credentials are ever returned); PATCH for administrators."""

    def get_permissions(self):
        return [IsAdministrator()] if self.request.method == 'PATCH' else [IsAuthenticated()]

    def get(self, request):
        return Response(EmailSettingsSerializer(EmailSettings.load()).data)

    def patch(self, request):
        config = EmailSettings.load()
        serializer = EmailSettingsSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(EmailSettingsSerializer(EmailSettings.load()).data)


class EmailTemplateViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = EmailTemplateSerializer
    queryset = EmailTemplate.objects.select_related('updated_by').order_by('category', 'name')
    lookup_field = 'key'
    permission_classes = [IsAdministratorOrReadOnly]
    pagination_class = None
    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def preview(self, request, key=None):
        template = self.get_object()
        serializer = TemplatePreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        application = None
        if data.get('application'):
            application = get_object_or_404(Application.objects.select_related('vendor__owner'), pk=data['application'])
        line = data.get('application_line')
        if line is not None and (application is None or line.application_id != application.pk):
            return Response({'application_line': 'Choose a line of the selected application.'}, status=400)
        overrides = {name: data[name] for name in (*services.TEMPLATE_PARTS, 'allowed_variables') if name in data}
        return Response(services.preview_template(
            template, application=application, application_line=line, market_weekend=data.get('market_weekend'),
            overrides=overrides,
        ))


class EmailMessageFilter(django_filters.FilterSet):
    template = django_filters.CharFilter(field_name='template_key')
    status = django_filters.CharFilter(method='filter_status', help_text='Comma-separated statuses.')
    bucket = django_filters.ChoiceFilter(method='filter_bucket', choices=[(key, key) for key in BUCKETS])

    class Meta:
        model = EmailMessage
        fields = ['application', 'vendor', 'market_weekend', 'application_line']

    def filter_status(self, queryset, name, value):
        return queryset.filter(status__in=[part.strip() for part in value.split(',') if part.strip()])

    def filter_bucket(self, queryset, name, value):
        return queryset.filter(status__in=BUCKETS[value])


class EmailMessageViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Message history (read-only) with the controlled actions: edit a draft, approve, cancel."""

    queryset = MESSAGES
    filterset_class = EmailMessageFilter
    ordering_fields = ['created_at', 'scheduled_for', 'sent_at']
    ordering = ['-created_at']
    search_fields = ['subject', 'recipient_email', 'vendor__brand_name']
    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    def get_serializer_class(self):
        return EmailMessageSerializer if self.action == 'list' else EmailMessageDetailSerializer

    def _detail(self, message):
        return Response(EmailMessageDetailSerializer(MESSAGES.get(pk=message.pk), context=self.get_serializer_context()).data)

    def partial_update(self, request, *args, **kwargs):
        """Edit a draft. The previous version is kept as a revision."""
        message = self.get_object()
        serializer = ReviseDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.revise_draft(
            message, actor=request.user, subject=data.get('subject', message.subject),
            html_body=data.get('html_body', message.html_body), text_body=data.get('text_body', message.text_body),
            note=data.get('note', ''),
        )
        return self._detail(message)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        message = self.get_object()
        services.approve_draft(message, actor=request.user)
        return self._detail(message)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        message = self.get_object()
        serializer = CancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.cancel_message(message, actor=request.user, reason=serializer.validated_data['reason'])
        return self._detail(message)

    @action(detail=True, methods=['get'])
    def revisions(self, request, pk=None):
        message = self.get_object()
        return Response(EmailRevisionSerializer(message.revisions.select_related('replaced_by'), many=True).data)


class EmailOverviewView(APIView):
    """Counts per bucket (each message counted once, by its current status) and recent email activity."""

    def get(self, request):
        totals = EmailMessage.objects.aggregate(**{
            bucket: Count('id', filter=Q(status__in=statuses)) for bucket, statuses in BUCKETS.items()
        })
        recent = (
            ActivityEvent.objects.filter(event_type__startswith='EMAIL_')
            .select_related('vendor', 'actor').order_by('-created_at')[:10]
        )
        return Response({
            'counts': totals,
            'by_status': dict(EmailMessage.objects.values_list('status').annotate(count=Count('id')).order_by()),
            'filters': {bucket: {'bucket': bucket} for bucket in BUCKETS},
            'recent_activity': [
                {
                    'id': str(event.pk), 'event_type': event.event_type, 'description': event.description,
                    'vendor': {'id': str(event.vendor_id), 'brand_name': event.vendor.brand_name},
                    'application': str(event.application_id) if event.application_id else None,
                    'email_message_id': event.metadata.get('email_message_id'),
                    'actor': event.actor.display_name if event.actor else 'System',
                    'created_at': event.created_at,
                }
                for event in recent
            ],
        })
