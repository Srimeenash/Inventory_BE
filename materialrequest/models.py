from datetime import datetime

from django.conf import settings
from django.db import models

from components.models import Component


def generate_material_request_id():
    now = datetime.now()
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    return f"MR-{date_part}-{time_part}"


class MaterialRequest(models.Model):
    REQUEST_TYPE_CHOICES = [
        ("BOM", "BOM"),
        ("R&D", "R&D"),
        ("RETURNABLE", "Returnable"),
        ("RETAIL_SALES", "Retail Sales"),
    ]

    RETURNABLE_PURPOSE_CHOICES = [
        ("FLIGHT_TEST", "Flight Test"),
        ("CUSTOMER_DEMO", "Customer Demo"),
        ("QC_CHECK", "QC Check"),
        ("EVENT", "Event"),
        ("MISCELLANEOUS_USAGE", "Miscellaneous Usage"),
    ]

    material_request_id = models.CharField(max_length=50, unique=True, blank=True, null=True)
    requester_name = models.CharField(max_length=100)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="material_requests",
    )
    date = models.DateField()

    # Project is required by validation only for BOM and R&D.
    project = models.CharField(max_length=100, blank=True, default="")
    bom = models.CharField(max_length=100, blank=True, null=True)
    customized_bom = models.BooleanField(default=False)

    request_type = models.CharField(
        max_length=20,
        choices=REQUEST_TYPE_CHOICES,
        default="BOM",
        db_index=True,
    )
    returnable_purpose = models.CharField(
        max_length=30,
        choices=RETURNABLE_PURPOSE_CHOICES,
        blank=True,
        default="",
        db_index=True,
    )

    # Kept for compatibility with existing BOM/summary screens.
    required_quantity = models.PositiveIntegerField(default=1)
    required_date = models.DateField()
    remarks = models.TextField(blank=True, null=True)

    status = models.CharField(
        max_length=40,
        choices=[
            ("PENDING", "Pending"),
            ("REQUESTED", "Requested"),
            ("PENDING_MANAGER", "Pending Manager"),
            ("MANAGER_APPROVED", "Manager Approved"),
            ("MANAGER_REJECTED", "Manager Rejected"),
            ("PROCUREMENT_PENDING", "Procurement Pending"),
            ("INVENTORY_PENDING", "Inventory Pending"),
            ("APPROVED", "Approved"),
            ("ORDERED", "Ordered"),
            ("PO_RAISED", "PO Raised"),
            ("PARTIALLY_DELIVERED", "Partially Delivered"),
            ("PO_DELIVERED", "PO Delivered"),
            ("QC_CHECKED", "QC Checked"),
            ("QC_FAILED_ACTION_REQUIRED", "QC Failed - Action Required"),
            ("AWAITING_REPLACEMENT_APPROVAL", "Awaiting Replacement Approval"),
            ("REPLACEMENT_APPROVAL_REJECTED", "Replacement Approval Rejected"),
            ("REPLACEMENT_APPROVED", "Replacement Approved"),
            ("AWAITING_REPLACEMENT_DELIVERY", "Awaiting Replacement Delivery"),
            ("REPLACEMENT_PARTIALLY_RECEIVED", "Replacement Partially Received"),
            ("REPLACEMENT_RECEIVED", "Replacement Received - QC Pending"),
            ("PROJECT_INVENTORY_READY", "Project Inventory Ready"),
            ("INVENTORY_ISSUED", "Inventory Issued"),
            ("MR_COMPLETED", "MR Completed"),
            ("REJECTED", "Rejected"),
            ("ORDER_DELIVERED", "Order Delivered - Legacy"),
        ],
        default="PENDING",
    )

    approval_status = models.CharField(
        max_length=30,
        choices=[
            ("PENDING", "Pending"),
            ("REQUESTED", "Requested"),
            ("PENDING_MANAGER", "Pending Manager"),
            ("MANAGER_APPROVED", "Manager Approved"),
            ("MANAGER_REJECTED", "Manager Rejected"),
            ("PO_RAISED", "PO Raised"),
        ],
        default="PENDING",
    )
    rejection_reason = models.TextField(blank=True, null=True, help_text="Reason for rejection")
    rejected_by = models.CharField(max_length=100, blank=True, null=True, help_text="Role or user who rejected")
    po_raised = models.BooleanField(
        default=False,
        help_text="Marks whether procurement has raised a purchase order for this request",
    )

    def save(self, *args, **kwargs):
        if not self.material_request_id:
            base_id = generate_material_request_id()
            candidate = base_id
            suffix = 1
            while MaterialRequest.objects.filter(material_request_id=candidate).exists():
                candidate = f"{base_id}-{suffix}"
                suffix += 1
            self.material_request_id = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.material_request_id or f"{self.project or self.request_type} - {self.requester_name}"


class BOMItem(models.Model):
    material_request = models.ForeignKey(MaterialRequest, related_name="bom_items", on_delete=models.CASCADE)
    component = models.ForeignKey(Component, on_delete=models.CASCADE, null=True, blank=True, related_name="material_request_bom_items")
    category = models.CharField(max_length=100, blank=True, null=True)
    specification = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=20, default="pc")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    inventory_quantity = models.PositiveIntegerField(default=0)
    po_raised_quantity = models.PositiveIntegerField(default=0)
    delivered_quantity = models.PositiveIntegerField(default=0)
    qc_passed_quantity = models.PositiveIntegerField(default=0)
    qc_failed_quantity = models.PositiveIntegerField(default=0)
    project_inventory_quantity = models.PositiveIntegerField(default=0)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.component} ({self.quantity})"


class RDItem(models.Model):
    material_request = models.ForeignKey(MaterialRequest, related_name="rd_items", on_delete=models.CASCADE)
    component = models.ForeignKey("components.Component", on_delete=models.CASCADE, null=True, blank=True)
    category = models.CharField(max_length=100, blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, default="pc")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    inventory_quantity = models.PositiveIntegerField(default=0)
    po_raised_quantity = models.PositiveIntegerField(default=0)
    delivered_quantity = models.PositiveIntegerField(default=0)
    qc_passed_quantity = models.PositiveIntegerField(default=0)
    qc_failed_quantity = models.PositiveIntegerField(default=0)
    project_inventory_quantity = models.PositiveIntegerField(default=0)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return str(self.component) if self.component else f"R&D Item - {self.material_request.material_request_id}"


class RequestItem(models.Model):
    """Component rows used only by RETURNABLE and RETAIL_SALES requests."""

    material_request = models.ForeignKey(
        MaterialRequest,
        related_name="request_items",
        on_delete=models.CASCADE,
    )
    component = models.ForeignKey(
        "components.Component",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="material_request_general_items",
    )
    category = models.CharField(max_length=100, blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=20, default="pc")
    inventory_quantity = models.PositiveIntegerField(default=0)
    po_raised_quantity = models.PositiveIntegerField(default=0)
    delivered_quantity = models.PositiveIntegerField(default=0)
    qc_passed_quantity = models.PositiveIntegerField(default=0)
    qc_failed_quantity = models.PositiveIntegerField(default=0)
    project_inventory_quantity = models.PositiveIntegerField(default=0)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.component or 'Component'} ({self.quantity})"
