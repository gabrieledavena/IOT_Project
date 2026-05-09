from django.urls import path
from .views import login_view, register_view, home_view, SolarCommunityView, PanelDataList, PanelDataDetail, PhotovoltaicSystemView
from django.contrib.auth.views import LogoutView

app_name = 'SP'

urlpatterns = [
    path('login/', login_view, name='login'),
    path('register/', register_view, name='register'),
    path('home/', home_view, name='home'),
    path('logout/', LogoutView.as_view(next_page='SP:login'), name='logout'),
    path('community/', SolarCommunityView.as_view(), name='solar_community'),
    path('system/<int:system_id>/', PhotovoltaicSystemView.as_view(), name='photovoltaic_system'),
    path("panel-data/", PanelDataList.as_view(), name="panel-data-list"),
    path("panel-data/<int:pk>/", PanelDataDetail.as_view(), name="panel-data-detail"),
]