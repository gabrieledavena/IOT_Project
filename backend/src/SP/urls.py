

from django.urls import path, include
from rest_framework import routers
router = routers.DefaultRouter()

from SP.views import PanelDataList, PanelDataDetail, SolarHistoryView
app_name = 'SP'

urlpatterns = [
    path('history/', SolarHistoryView.as_view(), name='solar-history'),

    path("panel-data/", PanelDataList.as_view(), name="panel-data-list"),
    path("panel-data/<int:pk>/", PanelDataDetail.as_view(), name="panel-data-detail"),
]