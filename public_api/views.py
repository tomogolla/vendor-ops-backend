"""Public endpoints under /api/v1/public/ (no login, no session, no CSRF).

Every endpoint answers 404 while PUBLIC_APPLICATIONS_ENABLED is off. Request bodies, tokens and
personal details are never logged."""

from django.conf import settings
from django.http import Http404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


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
