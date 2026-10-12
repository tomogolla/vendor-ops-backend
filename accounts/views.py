from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User
from .permissions import IsAdministrator
from .serializers import CurrentUserSerializer, LoginSerializer, UserAdminSerializer, UserSummarySerializer


@method_decorator(ensure_csrf_cookie, name='dispatch')
class CsrfView(APIView):
    """Sets the csrftoken cookie so the SPA can send X-CSRFToken on writes."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'csrfToken': get_token(request)})


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'login'

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request, **serializer.validated_data)
        if user is None:
            return Response({'detail': 'Invalid username or password.'}, status=status.HTTP_400_BAD_REQUEST)
        login(request, user)
        return Response(CurrentUserSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(CurrentUserSerializer(request.user).data)


class TeamView(APIView):
    """Active team members, for owner pickers. Any signed-in user may list them."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = User.objects.filter(is_active=True)
        return Response(UserSummarySerializer(users, many=True).data)


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserAdminSerializer
    permission_classes = [IsAdministrator]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    filterset_fields = ['role', 'is_active']
    search_fields = ['username', 'email', 'first_name', 'last_name']
