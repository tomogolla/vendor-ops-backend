from django.urls import path

from .views import PublicWeekendsView

urlpatterns = [
    path('market-weekends/', PublicWeekendsView.as_view(), name='public-market-weekends'),
]
