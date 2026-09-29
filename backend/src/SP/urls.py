from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import api, views

app_name = "SP"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="SP/login.html"), name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("community/", views.SolarCommunityView.as_view(), name="solar_community"),
    path("system/", views.PhotovoltaicSystemListView.as_view(), name="photovoltaic_system_list"),
    path("system/<int:system_id>/", views.PhotovoltaicSystemView.as_view(), name="photovoltaic_system"),
    path("panel-data/", api.PanelDataList.as_view(), name="panel_data_list"),
    path("panel-data/<int:pk>/", api.PanelDataDetail.as_view(), name="panel_data_detail"),
]
