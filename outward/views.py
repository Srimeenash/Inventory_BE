from rest_framework import viewsets
from .models import OutwardEntry
from .serializers import OutwardEntrySerializer


class OutwardEntryViewSet(viewsets.ModelViewSet):
    queryset = OutwardEntry.objects.all().order_by('-out_date')
    serializer_class = OutwardEntrySerializer
