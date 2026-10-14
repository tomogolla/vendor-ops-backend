from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.views import CsrfView, LoginView, LogoutView, MeView, TeamView, UserViewSet
from catalog.views import MarketWeekendViewSet
from imports.views import ImportConfirmView, ImportPreviewView
from tasks.views import TaskViewSet
from vendors.views import ApplicationViewSet, HealthView, VendorViewSet

router = DefaultRouter()
router.register('vendors', VendorViewSet, basename='vendor')
router.register('applications', ApplicationViewSet, basename='application')
router.register('tasks', TaskViewSet, basename='task')
router.register('users', UserViewSet, basename='user')
router.register('market-weekends', MarketWeekendViewSet, basename='market-weekend')

api_v1 = [
    path('health/', HealthView.as_view(), name='health'),
    path('auth/csrf/', CsrfView.as_view(), name='auth-csrf'),
    path('auth/login/', LoginView.as_view(), name='auth-login'),
    path('auth/logout/', LogoutView.as_view(), name='auth-logout'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    path('team/', TeamView.as_view(), name='team'),
    path('imports/vendors/preview/', ImportPreviewView.as_view(), name='import-preview'),
    path('imports/vendors/confirm/', ImportConfirmView.as_view(), name='import-confirm'),
    path('', include(router.urls)),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include(api_v1)),
]
