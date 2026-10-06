"""API REST delle misure dei pannelli: gli utenti le consultano, i dispositivi le inviano via MQTT (SP/mqtt.py)."""
from rest_framework import generics
from rest_framework.permissions import DjangoModelPermissions

from .models import PanelData
from .serializers import PanelDataSerializer


class PanelDataAccessMixin:
    serializer_class = PanelDataSerializer
    # Lettura per gli utenti autenticati; modifiche solo con i permessi change/delete su PanelData (i superuser)
    permission_classes = [DjangoModelPermissions]

    def get_queryset(self):
        queryset = PanelData.objects.order_by("-time_stamp", "-id")
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return queryset
        # Gli altri utenti vedono solo le misure degli impianti della propria community
        return queryset.filter(system__community__customers__user=user)


class PanelDataList(PanelDataAccessMixin, generics.ListAPIView):
    pass


class PanelDataDetail(PanelDataAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    pass
