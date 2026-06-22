# views.py
from rest_framework import viewsets
from .models import MaterialRequest
from .serializers import MaterialRequestSerializer

class MaterialRequestViewSet(viewsets.ModelViewSet):
    queryset = MaterialRequest.objects.all().order_by("-date")
    serializer_class = MaterialRequestSerializer
