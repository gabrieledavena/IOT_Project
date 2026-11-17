from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import api, communities, customers, installations, interventions, mqtt_auth, pairing, views

app_name = "SP"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="SP/login.html"), name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
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
    # Utenti appena registrati, che un tecnico associa a una community
    path("customers/", customers.PendingCustomerListView.as_view(), name="customer_assignment"),
    path("customers/<int:customer_id>/assign/", customers.AssignCommunityView.as_view(), name="customer_assign"),
    # Le community: anagrafica, master con i suoi nodi LoRa e impianti
    path("communities/", communities.CommunityListView.as_view(), name="community_list"),
    path("communities/new/", communities.NewCommunityView.as_view(), name="community_new"),
    path("communities/<int:community_id>/", communities.CommunityDetailView.as_view(), name="community_detail"),
    path("communities/<int:community_id>/edit/", communities.EditCommunityView.as_view(), name="community_edit"),
    path("communities/<int:community_id>/delete/", communities.DeleteCommunityView.as_view(),
         name="community_delete"),
    path("communities/<int:community_id>/pair/", pairing.CommunityPairingCodeView.as_view(), name="community_pair"),
    # L'installazione di un impianto: il suo sensore (dispositivo con le credenziali o nodo LoRa) e il suo stato
    path("installations/new/", installations.NewInstallationView.as_view(), name="installation_new"),
    path("installations/<int:system_id>/", installations.InstallationDetailView.as_view(), name="installation_detail"),
    path("installations/<int:system_id>/pair/", pairing.PairingCodeView.as_view(), name="installation_pair"),
    path("installations/<int:system_id>/delete/", installations.DeleteSystemView.as_view(), name="installation_delete"),
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
