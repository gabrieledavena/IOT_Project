from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import api, interventions, views

app_name = "SP"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="SP/login.html"), name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("dashboard/", views.UserDashboardView.as_view(), name="dashboard"),
    path("cities/", views.CityBenchmarkView.as_view(), name="city_benchmark"),
    path("community/", views.SolarCommunityView.as_view(), name="solar_community"),
    path("system/", views.PhotovoltaicSystemListView.as_view(), name="photovoltaic_system_list"),
    path("system/<int:system_id>/", views.PhotovoltaicSystemView.as_view(), name="photovoltaic_system"),
    path("system/<int:system_id>/intervention/", interventions.InterventionRequestView.as_view(), name="intervention_request"),
    path("interventions/", interventions.InterventionDashboardView.as_view(), name="intervention_dashboard"),
    path("interventions/<int:pk>/", interventions.InterventionDetailView.as_view(), name="intervention_detail"),
    path("interventions/<int:pk>/report/", interventions.InterventionReportView.as_view(), name="intervention_report"),
    path("panel-data/", api.PanelDataList.as_view(), name="panel_data_list"),
    path("panel-data/<int:pk>/", api.PanelDataDetail.as_view(), name="panel_data_detail"),
]
