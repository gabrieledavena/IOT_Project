# mia_app/urls.py
from django.urls import path
from .views import get_meteo_domani, view_test_previsione

urlpatterns = [
    path('predict/', view_test_previsione, name='predict'),
]