from django.db import models

class Notification(models.Model):

    CATEGORY_CHOICES = [
        ("PO", "Purchase Order"),
        ("MR", "Material Request"),
        ("CU", "Component Usage"),
        ("PROC", "Procurement"),
    ]

    STATUS_CHOICES = [
        ("REQUESTED", "Requested"),
        ("PENDING", "Pending"),
        ("PENDING_MANAGER", "Pending Manager"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("MANAGER_REJECTED", "Manager Rejected"),
        ("PENDING_FINANCE", "Pending Finance"),
        ("FINANCE_APPROVED", "Finance Approved"),
        ("FINANCE_REJECTED", "Finance Rejected"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]
    ROLE_CHOICES = [
        ("ADMIN", "Admin"),
        ("MANAGER", "Manager"),
        ("INVENTORY", "Inventory"),
        ("PROCUREMENT", "Procurement"),
        ("FINANCE", "Finance"),
    ]

    category = models.CharField(
        max_length=10,
        choices=CATEGORY_CHOICES,
        default="MR"
    )

    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, null=True)

    reference_id = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )
    receiver = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        blank=True,
        null=True,
    )

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.category} - {self.title}"
        