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
    ]

    STATUS_CHOICES = [
        # General request states
        ("REQUESTED", "Requested"),
        ("PENDING", "Pending"),

        # Manager flow
        ("PENDING_MANAGER", "Pending Manager"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("MANAGER_REJECTED", "Manager Rejected"),

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
        (
            "INVENTORY_CHECK_PENDING",
            "Inventory Check Pending",
        ),
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

    # Display/audit name of the user who originally created
    # or requested this notification.
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

    # Role-level destination.
    #
    # Examples:
    #   FINANCE -> every Finance user can see it
    #   MANAGER -> every Manager can see it
    #
    # For a notification addressed to one exact user,
    # receiver can remain NULL and recipient_user is used.
    receiver = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        blank=True,
        null=True,
        db_index=True,
    )

    # Exact-user destination.
    #
    # This is required for the Scrap workflow:
    #
    # Inventory / Engineer creates Scrap
    #        ↓
    # Finance approves
    #        ↓
    # Manager approves
    #        ↓
    # Final notification goes ONLY to the user
    # who originally created that Scrap.
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="ipms_notifications",
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
            # Fast role-based notification lookup.
            models.Index(
                fields=[
                    "receiver",
                    "category",
                    "status",
                ],
                name="notif_recv_stat_idx",
            ),

            # Fast workflow-reference lookup.
            models.Index(
                fields=[
                    "category",
                    "reference_id",
                    "receiver",
                ],
                name="notif_ref_recv_idx",
            ),

            # Fast exact-user notification lookup.
            models.Index(
                fields=[
                    "recipient_user",
                    "category",
                    "status",
                ],
                name="notif_user_cat_stat_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.category} - "
            f"{self.title}"
        )