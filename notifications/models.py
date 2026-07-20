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
        ("PENDING_ADMIN", "Pending Admin"),
        ("PENDING_MANAGER", "Pending Manager"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
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

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.category} - {self.title}"
        