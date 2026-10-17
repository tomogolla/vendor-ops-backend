from django.urls import path

from .views import PublicApplicationView, PublicWeekendsView

urlpatterns = [
    path('market-weekends/', PublicWeekendsView.as_view(), name='public-market-weekends'),
    path('applications/', PublicApplicationView.as_view(), name='public-applications'),
]
