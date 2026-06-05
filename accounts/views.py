from django.contrib.auth import get_user_model
from rest_framework import filters, viewsets
from django_filters.rest_framework import DjangoFilterBackend
from .permissions import IsAdmin
from .serializers import UserSerializer


User = get_user_model()


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.select_related('role').all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'role__name', 'department', 'designation']
    search_fields = ['username', 'email', 'first_name', 'last_name', 'department', 'designation']
    ordering_fields = ['username', 'email', 'date_joined']
    ordering = ['-date_joined']
