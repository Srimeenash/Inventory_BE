from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class ApprovalRequest(models.Model):
    MODULE_CHOICES = [
        ('PURCHASE_REQUEST', 'Purchase Request'),
        ('PURCHASE_ORDER', 'Purchase Order'),
        ('STOCK_OUT', 'Stock Out'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    module = models.CharField(max_length=50, choices=MODULE_CHOICES)
    reference_id = models.IntegerField()  # PR / PO / StockOut ID
    requested_by = models.CharField(max_length=100)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')

    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_requests'
    )

    remarks = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.module} - {self.status}"