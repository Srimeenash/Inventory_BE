from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import Project


User = get_user_model()


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = "__all__"
