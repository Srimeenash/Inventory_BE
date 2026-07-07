from rest_framework import filters, viewsets
from django_filters.rest_framework import DjangoFilterBackend
from .models import Project
from .serializers import ProjectSerializer


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.prefetch_related('team').all().order_by('-created_at')
    serializer_class = ProjectSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'status', 'department']
    search_fields = ['project_code', 'name', 'description', 'department']
    ordering_fields = ['project_code', 'name', 'start_date', 'end_date', 'created_at']
    ordering = ['-created_at']
