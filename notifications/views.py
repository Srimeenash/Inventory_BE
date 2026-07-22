from rest_framework import viewsets
from .models import Notification
from .serializers import NotificationSerializer

class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        queryset = Notification.objects.all().order_by("-created_at")

        category = self.request.query_params.get("category")

        if category:
            queryset = queryset.filter(category=category)

        return queryset