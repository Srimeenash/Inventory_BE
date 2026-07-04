from rest_framework import viewsets, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import InwardEntry
from .serializers import InwardEntrySerializer


class InwardQCSerializer(serializers.Serializer):
    passedRows = serializers.ListField(child=serializers.DictField(), required=False)
    failedRows = serializers.ListField(child=serializers.DictField(), required=False)
    timestamp = serializers.DateTimeField(required=False)


class InwardEntryViewSet(viewsets.ModelViewSet):
    queryset = InwardEntry.objects.all().order_by('-received_date')
    serializer_class = InwardEntrySerializer

    @action(detail=True, methods=['post'], url_path='qc')
    def qc(self, request, pk=None):
        inward_entry = self.get_object()
        serializer = InwardQCSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        passed_rows = serializer.validated_data.get('passedRows', [])
        failed_rows = serializer.validated_data.get('failedRows', [])

        passed_rows = serializer.validated_data.get("passedRows", [])
        failed_rows = serializer.validated_data.get("failedRows", [])

        inspected_count = len(passed_rows) + len(failed_rows)

        if inspected_count == inward_entry.quantity_received:
            inward_entry.qc_status = "COMPLETED"
        else:
            inward_entry.qc_status = "PENDING"

        inward_entry.qc_passed_rows = passed_rows
        inward_entry.qc_failed_rows = failed_rows
        inward_entry.qc_timestamp = serializer.validated_data.get("timestamp")
        inward_entry.save()
        return Response(
            {
                'id': inward_entry.id,
                'qc_status': inward_entry.qc_status,
                'passedRows': inward_entry.qc_passed_rows,
                'failedRows': inward_entry.qc_failed_rows,
                'timestamp': inward_entry.qc_timestamp,
            },
            status=status.HTTP_200_OK,
        )
