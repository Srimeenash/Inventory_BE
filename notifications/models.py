from django.conf import settings
from django.db import models


class Notification(models.Model):
    CATEGORY_CHOICES = [
        ("PO", "Purchase Order"),
        ("MR", "Material Request"),
        ("CU", "Component Usage"),
        ("PROC", "Procurement"),
        ("SCRAP", "Scrap"),
        ("BOM", "BOM"),
        ("SALES", "Sales"),
    ]

    STATUS_CHOICES = [
        # General request states
        ("REQUESTED", "Requested"),
        ("PENDING", "Pending"),

        # Manager flow
        ("PENDING_MANAGER", "Pending Manager"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("MANAGER_REJECTED", "Manager Rejected"),

        # Management / Sales flow
        ("PENDING_MANAGEMENT", "Pending Management"),
        ("MANAGEMENT_APPROVED", "Management Approved"),
        ("MANAGEMENT_REJECTED", "Management Rejected"),

        # Procurement / MR flow
        ("PROCUREMENT_PENDING", "Procurement Pending"),
        ("PO_RAISED", "PO Raised"),
        ("PO_DELIVERED", "PO Delivered"),

        # Finance flow
        ("PENDING_FINANCE", "Pending Finance"),
        ("FINANCE_APPROVED", "Finance Approved"),
        ("FINANCE_REJECTED", "Finance Rejected"),

        # QC / Inventory flow
        ("INVENTORY_PENDING", "Inventory Pending"),
        ("INVENTORY_CHECK_PENDING", "Inventory Check Pending"),
        ("QC_CHECKED", "QC Checked"),
        (
            "PROJECT_INVENTORY_READY",
            "Project Inventory Ready",
        ),
        ("INVENTORY_ISSUED", "Inventory Issued"),
        ("MR_COMPLETED", "MR Completed"),

        # Generic final states
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    ROLE_CHOICES = [
        ("ADMIN", "Admin"),
        ("MANAGER", "Manager"),
        ("PROCUREMENT", "Procurement"),
        ("INVENTORY", "Inventory"),
        ("FINANCE", "Finance"),
        ("MANAGEMENT", "Management"),
    ]

    category = models.CharField(
        max_length=10,
        choices=CATEGORY_CHOICES,
        default="MR",
        db_index=True,
    )

    title = models.CharField(
        max_length=255,
    )

    message = models.TextField(
        blank=True,
        null=True,
    )

    reference_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )

    # Exact-user notification target.
    #
    # Existing notifications/views.py uses:
    #   select_related("recipient_user")
    #   Q(recipient_user__isnull=True) | Q(recipient_user=request.user)
    #
    # Scrap final-result notifications also create Notification rows with
    # recipient_user=<original requester>. Keep this field even though most
    # approval notifications are role-level and use receiver instead.
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ipms_received_notifications",
    )

    # User name that created/requested the notification.
    # This is what the Manager Scrap table displays.
    requested_by = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        db_index=True,
    )

    status = models.CharField(
        max_length=40,
        choices=STATUS_CHOICES,
        default="PENDING",
        db_index=True,
    )

    receiver = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        blank=True,
        null=True,
        db_index=True,
    )

    is_read = models.BooleanField(
        default=False,
        db_index=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        ordering = [
            "-created_at",
            "-id",
        ]

        indexes = [
            models.Index(
                fields=[
                    "receiver",
                    "category",
                    "status",
                ],
                name="notif_recv_stat_idx",
            ),
            models.Index(
                fields=[
                    "category",
                    "reference_id",
                    "receiver",
                ],
                name="notif_ref_recv_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.category} - "
            f"{self.title}"
        )
