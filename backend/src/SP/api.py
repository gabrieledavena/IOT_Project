"""API REST delle misure dei pannelli: il bridge Arduino le invia, gli utenti le consultano."""
from rest_framework import generics
from rest_framework.permissions import DjangoModelPermissions

from .models import PanelData
from .serializers import PanelDataSerializer


class PanelDataAccessMixin:
    serializer_class = PanelDataSerializer
    # Lettura per gli utenti autenticati; scrittura solo con i permessi add/change/delete
    # su PanelData (l'utente del bridge ha solo add, i superuser tutti)
    permission_classes = [DjangoModelPermissions]

    def get_queryset(self):
        queryset = PanelData.objects.order_by("-time_stamp", "-id")
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return queryset
        # Gli altri utenti vedono solo le misure degli impianti della propria community
        return queryset.filter(system__community__customers__user=user)


class PanelDataList(PanelDataAccessMixin, generics.ListCreateAPIView):
    pass


class PanelDataDetail(PanelDataAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    pass
