from rest_framework import serializers
from .models import ReportLog


class ReportLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportLog
        fields = '__all__'
        read_only_fields = ['created_at']