

from django.urls import path, include
from rest_framework import routers
router = routers.DefaultRouter()

from SP.views import PanelDataList, PanelDataDetail, SolarHistoryView
app_name = 'SP'

urlpatterns = [
    path("panel-data/", PanelDataList.as_view()),
    path("panel-data/<int:pk>/", PanelDataDetail.as_view()),
    path('history/', SolarHistoryView.as_view(), name='solar-history'),

]