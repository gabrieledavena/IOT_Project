from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import api, gateways, installations, interventions, mqtt_auth, pairing, views

app_name = "SP"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="SP/login.html"), name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("cities/", views.CityBenchmarkView.as_view(), name="city_benchmark"),
    path("community/", views.SolarCommunityView.as_view(), name="solar_community"),
    path("community/energy/", views.EnergySummaryView.as_view(), name="community_energy"),
    path("system/", views.PhotovoltaicSystemListView.as_view(), name="photovoltaic_system_list"),
    path("system/<int:system_id>/", views.PhotovoltaicSystemView.as_view(), name="photovoltaic_system"),
    path("system/<int:system_id>/energy/", views.EnergySummaryView.as_view(), name="system_energy"),
    path("system/<int:system_id>/intervention/", interventions.InterventionRequestView.as_view(), name="intervention_request"),
    path("interventions/", interventions.InterventionDashboardView.as_view(), name="intervention_dashboard"),
    path("interventions/<int:pk>/", interventions.InterventionDetailView.as_view(), name="intervention_detail"),
    path("interventions/<int:pk>/report/", interventions.InterventionReportView.as_view(), name="intervention_report"),
    path("installations/", installations.InstallationListView.as_view(), name="installation_list"),
    path("installations/new/", installations.NewInstallationView.as_view(), name="installation_new"),
    path("installations/<int:system_id>/", installations.InstallationDetailView.as_view(), name="installation_detail"),
    path("installations/<int:system_id>/pair/", pairing.PairingCodeView.as_view(), name="installation_pair"),
    # Master delle community e i loro nodi LoRa
    path("installations/communities/", gateways.CommunityListView.as_view(), name="community_list"),
    path("installations/communities/<int:community_id>/", gateways.CommunityInstallationView.as_view(),
         name="community_installation"),
    path("installations/communities/<int:community_id>/pair/", pairing.CommunityPairingCodeView.as_view(),
         name="community_pair"),
    # Installazione con il QR del display: la pagina aperta dal QR (indirizzo corto, perché il QR resti piccolo)
    # e le richieste del dispositivo
    path("pair/<str:code>/", pairing.PairingView.as_view(), name="pair"),
    path("pair/<str:code>/new/", pairing.PairedInstallationView.as_view(), name="pair_new"),
    path("devices/pairing/", pairing.start, name="pairing_start"),
    path("devices/pairing/credentials/", pairing.credentials, name="pairing_credentials"),
    path("panel-data/", api.PanelDataList.as_view(), name="panel_data_list"),
    path("panel-data/<int:pk>/", api.PanelDataDetail.as_view(), name="panel_data_detail"),
    # Chiamate dal broker MQTT (mosquitto-go-auth) per decidere chi può collegarsi e a quali topic
    path("mqtt/auth/user/", mqtt_auth.user, name="mqtt_auth_user"),
    path("mqtt/auth/superuser/", mqtt_auth.superuser, name="mqtt_auth_superuser"),
    path("mqtt/auth/acl/", mqtt_auth.acl, name="mqtt_auth_acl"),
]
