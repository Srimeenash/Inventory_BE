from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import Project


User = get_user_model()


class ProjectSerializer(serializers.ModelSerializer):
    manager_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        source='manager',
        write_only=True,
        required=False,
        allow_null=True,
    )
    team_ids = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        source='team',
        many=True,
        write_only=True,
        required=False,
    )
    manager = serializers.PrimaryKeyRelatedField(read_only=True)
    team = serializers.PrimaryKeyRelatedField(read_only=True, many=True)

    class Meta:
        model = Project
        fields = [
            'id',
            'project_code',
            'name',
            'description',
            'department',
            'status',
            'budget',
            'start_date',
            'end_date',
            'manager',
            'manager_id',
            'team',
            'team_ids',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'manager', 'team']
