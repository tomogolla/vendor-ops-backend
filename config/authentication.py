from rest_framework.authentication import SessionAuthentication


class SessionAuthentication401(SessionAuthentication):
    """Session auth that answers unauthenticated requests with 401 instead of 403, so the
    frontend can tell "please sign in" apart from "not allowed"."""

    def authenticate_header(self, request):
        return 'Session'
