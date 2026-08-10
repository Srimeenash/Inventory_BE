from django.db import models
from components.models import Component


class OutwardEntry(models.Model):
    OUTWARD_TYPE_CHOICES = [
        ("SCRAP", "Scrap"),
        ("SALES", "Sales"),
        ("EVENT", "Event"),
    ]

    ITEM_TYPE_CHOICES = [
        ("COMPONENT", "Component"),
        ("DRONE", "Drone"),
    ]

    APPROVAL_STATUS_CHOICES = [
        ("NOT_REQUESTED", "Not Requested"),
        ("REQUESTED", "Requested"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    code = models.CharField(max_length=100, unique=True)
    outward_type = models.CharField(
        max_length=20,
        choices=OUTWARD_TYPE_CHOICES,
        default="SCRAP",
    )
    item_type = models.CharField(
        max_length=20,
        choices=ITEM_TYPE_CHOICES,
        default="COMPONENT",
    )

    out_date = models.DateField()
    time = models.TimeField(blank=True, null=True)

    product_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="outward_entries",
    )

    # Actual quantity represented by this Outward row.
    quantity = models.PositiveIntegerField(default=1)

    # Exact serials removed from In Store for SALES/EVENT component rows.
    serial_numbers = models.JSONField(default=list, blank=True)

    # Exact source Inventory rows used during FIFO deduction.
    # This is required so EVENT returns can restore the same serials safely.
    inventory_allocations = models.JSONField(default=list, blank=True)

    # EVENT return tracking. Only returned-good quantity/serials are restored.
    returned_quantity = models.PositiveIntegerField(default=0)
    returned_serial_numbers = models.JSONField(default=list, blank=True)
    stock_deducted = models.BooleanField(default=False)
    stock_restored = models.BooleanField(default=False)

    invoice_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    client = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )
    deliverables = models.TextField(blank=True, null=True)
    gate_pass = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    event_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )
    no_of_components = models.PositiveIntegerField(
        blank=True,
        null=True,
    )
    return_date = models.DateField(blank=True, null=True)
    drone_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )
    attendee_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )
    event_components = models.TextField(blank=True, null=True)
    is_returned = models.BooleanField(default=False)

    remarks = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        default="NEW",
    )

    # Retained only for compatibility with existing rows/screens.
    # New direct Sales/Event entries always use NOT_REQUESTED.
    approval_status = models.CharField(
        max_length=30,
        choices=APPROVAL_STATUS_CHOICES,
        default="NOT_REQUESTED",
    )
    rejection_reason = models.TextField(blank=True, null=True)
    rejected_by = models.CharField(
        max_length=50,
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-out_date", "-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["outward_type", "item_type", "status"],
                name="out_type_item_status_idx",
            ),
            models.Index(
                fields=["component", "outward_type"],
                name="out_comp_type_idx",
            ),
        ]

    def __str__(self):
        return self.code