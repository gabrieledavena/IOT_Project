from django.contrib import admin
from django.urls import include, path

from SP.views import home

urlpatterns = [
    # Vetrina dei servizi per i visitatori, dashboard degli impianti dopo il login
    path("", home, name="home"),
    path("admin/", admin.site.urls),
    path("sp/", include("SP.urls")),
    path("forecast/", include("forecast.urls")),
    path("roi/", include("roi.urls")),
]
