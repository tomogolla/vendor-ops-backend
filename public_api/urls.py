from django.urls import path

from .sheets import GoogleSheetsApplicationView
from .views import PublicApplicationView, PublicWeekendsView

urlpatterns = [
    path('market-weekends/', PublicWeekendsView.as_view(), name='public-market-weekends'),
    path('applications/', PublicApplicationView.as_view(), name='public-applications'),
    path('google-sheets/applications/', GoogleSheetsApplicationView.as_view(), name='google-sheets-applications'),
]
