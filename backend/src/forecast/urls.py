# mia_app/urls.py
from django.urls import path
from .views import get_weather_data, view_test_previsione, view_test_previsione_oggi, view_test_previsione_rf

urlpatterns = [
    path('tomorrow/', view_test_previsione, name='tomorrow'),
    path('today/', view_test_previsione_oggi, name='today'),
    path('rf/', view_test_previsione_rf, name='rf'),
]