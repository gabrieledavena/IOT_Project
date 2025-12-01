from rest_framework import serializers
from SP.models import PanelData

class PanelDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = PanelData
        fields = '__all__'