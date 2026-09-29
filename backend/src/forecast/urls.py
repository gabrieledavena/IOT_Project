from django.urls import path

from .views import forecast_view

app_name = "forecast"

urlpatterns = [
    path("today/", forecast_view, {"days_ahead": 0}, name="today"),
    path("tomorrow/", forecast_view, {"days_ahead": 1}, name="tomorrow"),
]
