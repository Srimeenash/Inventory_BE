from rest_framework import viewsets
from .models import MaterialRequest
from .serializers import MaterialRequestSerializer
from notifications.models import Notification   # ✅ ADD THIS

class MaterialRequestViewSet(viewsets.ModelViewSet):
    queryset = MaterialRequest.objects.all().order_by("-date")
    serializer_class = MaterialRequestSerializer
    pagination_class = None  
    # ✅ ADD THIS METHOD
    def perform_create(self, serializer):
        instance = serializer.save()

        # ✅ ONLY CREATE NOTIFICATION IF USER IS LOGGED IN
        if self.request.user and self.request.user.is_authenticated:
            Notification.objects.create(
                recipient=self.request.user,
                title="New Material Request",
                message=f"{self.request.user.username} created material request",
                notification_type="INFO",
                reference_module="MR",
                reference_id=instance.id
            )