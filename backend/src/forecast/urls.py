from django.urls import path

from .views import forecast_view

app_name = "forecast"

urlpatterns = [
    path("", forecast_view, name="index"),
]
