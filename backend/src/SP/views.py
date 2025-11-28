from django.shortcuts import render

# Create your views here.
from SP.models import PanelData
from SP.serializers import PanelDataSerializer
from django.http import Http404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from rest_framework import generics


class PanelDataList(generics.ListCreateAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


class PanelDataDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer