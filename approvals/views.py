from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import ApprovalRequest
from .serializers import ApprovalRequestSerializer



class ApprovalRequestViewSet(viewsets.ModelViewSet):
    queryset = ApprovalRequest.objects.all().order_by('-created_at')
    serializer_class = ApprovalRequestSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        approval = self.get_object()
        approval.status = 'APPROVED'
        approval.approved_by = request.user
        approval.save()
        return Response({"message": "Approved successfully"})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        approval = self.get_object()
        approval.status = 'REJECTED'
        approval.approved_by = request.user
        approval.save()
        return Response({"message": "Rejected successfully"})