from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdministrator, is_admin
from tasks.models import Task

from . import pipeline, services
from .filters import ApplicationFilter, VendorFilter
from .models import Application, Vendor
from .serializers import (
    VENDOR_EDITABLE_FIELDS,
    ActivityEventSerializer,
    ApplicationSerializer,
    ApplicationStageChangeSerializer,
    ApplicationStatusChangeSerializer,
    DiscountSerializer,
    InternalNoteSerializer,
    QualificationChangeSerializer,
    VendorSerializer,
    VendorStatusChangeSerializer,
    match_payload,
)

DISCOUNT_FIELDS = {'discount_type', 'discount_value', 'discount_label'}
# DEPRECATED (v1.3): legacy status values accepted by PATCH /applications/{id}/.
LEGACY_STATUS_TO_STAGE = {
    Application.Status.SUBMITTED: Application.Stage.NEW,
    Application.Status.IN_REVIEW: Application.Stage.IN_REVIEW,
    Application.Status.DECLINED: Application.Stage.DECLINED,
    Application.Status.WITHDRAWN: Application.Stage.WITHDRAWN,
}


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return Response({'status': 'ok'})


def truthy(value) -> bool:
    return str(value).lower() in {'1', 'true', 'yes'}


class VendorViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    serializer_class = VendorSerializer
    filterset_class = VendorFilter
    search_fields = ['brand_name', 'first_name', 'last_name', 'email', 'instagram', 'phone']
    ordering_fields = ['created_at', 'updated_at', 'brand_name']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return Vendor.objects.select_related('owner', 'created_by').prefetch_related(
            Prefetch(
                'tasks',
                queryset=Task.objects.filter(task_type=Task.TaskType.DISCOVERY_CALL).order_by('-created_at'),
                to_attr='discovery_calls',
            )
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        owner = data.pop('owner', None)
        for key in ('is_active', 'outreach_opt_out'):
            data.pop(key, None)
        try:
            vendor = services.register_lead(
                actor=request.user, data=data, owner=owner,
                confirm_duplicate=truthy(request.data.get('confirm_duplicate')),
            )
        except services.DuplicateVendor as duplicate:
            return Response(
                {'detail': 'A vendor with the same email, phone or Instagram already exists.',
                 'code': 'possible_duplicate', **match_payload(duplicate.result)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(self.get_serializer(self.get_queryset().get(pk=vendor.pk)).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def duplicates(self, request):
        params = request.query_params
        result = services.find_matches(
            email=params.get('email', ''), phone=params.get('phone', ''), instagram=params.get('instagram', ''),
            brand_name=params.get('brand_name', ''), exclude_id=params.get('exclude') or None,
        )
        return Response(match_payload(result))

    @action(detail=True, methods=['post'], url_path='status')
    def change_status(self, request, pk=None):
        vendor = self.get_object()
        serializer = VendorStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.change_vendor_status(
            vendor, serializer.validated_data['status'], actor=request.user,
            note=serializer.validated_data.get('note', ''),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=vendor.pk)).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdministrator])
    def qualification(self, request, pk=None):
        vendor = self.get_object()
        serializer = QualificationChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_qualification(
            vendor, serializer.validated_data['qualification_status'], actor=request.user,
            reason=serializer.validated_data.get('reason', ''),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=vendor.pk)).data)

    @action(detail=True, methods=['get'])
    def activity(self, request, pk=None):
        vendor = self.get_object()
        events = vendor.activity.select_related('actor').order_by('-created_at')
        page = self.paginate_queryset(events)
        return self.get_paginated_response(ActivityEventSerializer(page, many=True).data)

    @action(detail=True, methods=['get', 'post'])
    def notes(self, request, pk=None):
        vendor = self.get_object()
        if request.method == 'POST':
            serializer = InternalNoteSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            application = serializer.validated_data.get('application')
            if application and application.vendor_id != vendor.pk:
                return Response({'application': 'That application belongs to another vendor.'}, status=400)
            note = services.add_note(
                vendor, actor=request.user, body=serializer.validated_data['body'],
                note_type=serializer.validated_data.get('note_type', 'GENERAL'), application=application,
            )
            return Response(InternalNoteSerializer(note).data, status=status.HTTP_201_CREATED)
        notes = vendor.internal_notes.select_related('author')
        page = self.paginate_queryset(notes)
        return self.get_paginated_response(InternalNoteSerializer(page, many=True).data)


class ApplicationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    serializer_class = ApplicationSerializer
    filterset_class = ApplicationFilter
    ordering_fields = ['submitted_at', 'updated_at']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return Application.objects.select_related('vendor', 'reviewer', 'submitted_by')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        vendor = data.pop('vendor', None)
        raw_vendor_data = data.pop('vendor_data', None)
        if request.data.get('status') not in (None, Application.Status.SUBMITTED) or request.data.get('stage'):
            raise PermissionDenied('New applications start as submitted.')
        reviewer = data.pop('reviewer', None)
        if reviewer is not None and not is_admin(request.user):
            raise PermissionDenied('Only administrators can assign reviewers.')
        source = data.pop('source', '') or 'Staff entry'

        vendor_data = None
        if vendor is None:
            vendor_serializer = VendorSerializer(data=raw_vendor_data, context=self.get_serializer_context())
            vendor_serializer.is_valid(raise_exception=True)
            vendor_data = {k: v for k, v in vendor_serializer.validated_data.items() if k in VENDOR_EDITABLE_FIELDS}
        try:
            application = services.submit_application(
                actor=request.user, vendor=vendor, vendor_data=vendor_data,
                application_data={**data, 'reviewer': reviewer} if reviewer else data, source=source,
            )
        except services.AmbiguousVendorMatch as ambiguous:
            return Response(
                {'detail': 'These contact details match more than one vendor. Choose the vendor explicitly.',
                 'code': 'ambiguous_vendor', **match_payload(ambiguous.result)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(self.get_serializer(application).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        application = self.get_object()
        protected = {'status', 'reviewer', 'decision_reason', *DISCOUNT_FIELDS} & request.data.keys()
        if protected and not is_admin(request.user):
            raise PermissionDenied('Only administrators can change status, reviewer, decision or discount.')
        if 'status' in request.data:
            # DEPRECATED (v1.3): legacy status writes are translated onto stages.
            change = ApplicationStatusChangeSerializer(
                data={'status': request.data['status'], 'reason': request.data.get('decision_reason', '')}
            )
            change.is_valid(raise_exception=True)
            new_status = change.validated_data['status']
            if new_status != application.derived_status:
                if new_status in (Application.Status.APPROVED, Application.Status.WAITLISTED):
                    raise services.UseLineDecisions()
                pipeline.change_application_stage(
                    application, LEGACY_STATUS_TO_STAGE[new_status], request.user,
                    reason=change.validated_data.get('reason', ''),
                )
        if DISCOUNT_FIELDS & request.data.keys():
            discount = DiscountSerializer(data={
                'discount_type': request.data.get('discount_type', application.discount_type),
                'discount_value': request.data.get('discount_value', application.discount_value),
                'discount_label': request.data.get('discount_label', application.discount_label),
            })
            discount.is_valid(raise_exception=True)
            pipeline.set_discount(
                application, actor=request.user, discount_type=discount.validated_data['discount_type'],
                value=discount.validated_data['discount_value'], label=discount.validated_data['discount_label'],
            )
        skip = {'status', 'decision_reason', 'vendor_data', *DISCOUNT_FIELDS}
        data = {k: v for k, v in request.data.items() if k not in skip}
        serializer = self.get_serializer(application, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self.get_serializer(self.get_queryset().get(pk=application.pk)).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdministrator])
    def stage(self, request, pk=None):
        application = self.get_object()
        serializer = ApplicationStageChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pipeline.change_application_stage(
            application, serializer.validated_data['stage'], request.user,
            reason=serializer.validated_data.get('reason', ''),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=application.pk)).data)
