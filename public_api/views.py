"""Public endpoints under /api/v1/public/ (no login, no session, no CSRF).

Every endpoint answers 404 while PUBLIC_APPLICATIONS_ENABLED is off. Request bodies, tokens and
personal details are never logged."""

import logging

from django.conf import settings
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import protection, services, turnstile
from .attribution import intake_metadata
from .serializers import PublicApplicationSerializer

logger = logging.getLogger(__name__)

# The same answer for every accepted submission (new vendor, existing vendor, bundled, ambiguous or
# a bot caught by the honeypot), so the form never reveals what the system already knows.
RECEIVED = {'status': 'received'}
NOT_PROCESSED = {'detail': 'We could not process your application. Please try again, or email us if this keeps happening.'}
NOT_VERIFIED = {'detail': 'We could not verify that you are not a robot. Please try again.'}
UNAVAILABLE = {'detail': 'Applications are temporarily unavailable. Please try again later, or email us.'}
EMAIL_LIMIT = {'detail': 'We have already received several applications from this email address today. Please try again tomorrow, or email us.'}


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def initial(self, request, *args, **kwargs):
        # The kill switch comes before authentication, permissions and throttling.
        if not settings.PUBLIC_APPLICATIONS_ENABLED:
            raise Http404
        super().initial(request, *args, **kwargs)


class PublicWeekendsView(PublicView):
    """Open market weekends with their booth sizes and waitlist flags (no prices or counts)."""

    throttle_scope = 'public_weekends'

    def get(self, request):
        response = Response(services.public_weekends())
        response['Cache-Control'] = 'public, max-age=60'
        return response


def client_ip(request) -> str | None:
    """The address nginx appended to X-Forwarded-For (NUM_PROXIES = 1), when it is a real IP."""
    return turnstile.reliable_ip(ScopedRateThrottle().get_ident(request))


class PublicApplicationView(PublicView):
    """POST /api/v1/public/applications/: a vendor applies from the public page.

    Order: kill switch, per-IP throttle, honeypot, validation, per-email limit, daily cap,
    Turnstile, then the submission (atomic)."""

    throttle_scope = 'public_apply'

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response(NOT_PROCESSED, status=status.HTTP_400_BAD_REQUEST)
        if str(request.data.get('website') or '').strip():
            logger.info('Public application ignored: honeypot field filled')
            return Response(RECEIVED)
        serializer = PublicApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if protection.email_limit_reached(data['email']):
            logger.info('Public application refused: daily limit for one email address')
            return Response(EMAIL_LIMIT, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if protection.daily_cap_reached(services.SOURCE):
            logger.warning('Public application refused: daily cap of %s reached', settings.PUBLIC_APPLICATIONS_DAILY_CAP)
            return Response(UNAVAILABLE, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            turnstile.verify(data['turnstile_token'], client_ip(request))
        except turnstile.TurnstileFailed:
            return Response(NOT_VERIFIED, status=status.HTTP_400_BAD_REQUEST)
        except turnstile.TurnstileUnavailable:
            return Response(UNAVAILABLE, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        metadata = intake_metadata(
            attribution=data['attribution'], user_agent=request.META.get('HTTP_USER_AGENT', ''),
            consent_version=data['consent_text_version'] or settings.PUBLIC_APPLICATION_CONSENT_VERSION,
        )
        try:
            application = services.submit_public_application(data, metadata)
        except ValidationError:
            # A rule refused it (nothing was saved). Its message may describe existing records, so
            # the applicant only gets a generic answer.
            logger.warning('Public application refused by a pipeline rule; nothing was saved')
            return Response(NOT_PROCESSED, status=status.HTTP_400_BAD_REQUEST)
        protection.record_email_submission(data['email'])
        logger.info('Public application received: application %s', application.pk)
        return Response(RECEIVED)
