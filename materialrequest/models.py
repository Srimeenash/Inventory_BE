# models.py
from datetime import datetime

from django.db import models
from components.models import Component


def generate_material_request_id():
    now = datetime.now()
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    return f"MR-{date_part}-{time_part}"


class MaterialRequest(models.Model):
    material_request_id = models.CharField(max_length=50, unique=True, blank=True, null=True)
    requester_name = models.CharField(max_length=100)
    
    date = models.DateField()
    project = models.CharField(max_length=100)
    bom = models.CharField(
    max_length=100,
    blank=True,
    null=True
)
    customized_bom = models.BooleanField(default=False)
    request_type = models.CharField(
        max_length=10,
        choices=[
            ("BOM", "BOM"),
            ("R&D", "R&D"),
        ],
        default="BOM",
    )
    required_quantity = models.PositiveIntegerField()
    required_date = models.DateField()
    remarks = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=30,
        choices=[
            ("PENDING", "Pending"),
            ("PENDING_MANAGER", "Pending Manager"),
            ("APPROVED", "Approved"),
            ("ORDERED", "Ordered"),
            ("ORDER_DELIVERED", "Order Delivered"),
            ("REJECTED", "Rejected"),
            ("PO_RAISED", "PO Raised"),
        ],
        default="PENDING",
    )
    approval_status = models.CharField(
        max_length=30,
        choices=[
            ("PENDING", "Pending"),
            ("REQUESTED", "Requested"),
            ("MANAGER_APPROVED", "Manager Approved"),
            ("MANAGER_REJECTED", "Manager Rejected"),
            ("ADMIN_APPROVED", "Admin Approved"),
            ("ADMIN_REJECTED", "Admin Rejected"),
            ("PO_RAISED", "PO Raised"),
        ],
        default="PENDING",
    )
    
    rejection_reason = models.TextField(blank=True, null=True, help_text="Reason for rejection")
    
    rejected_by = models.CharField(max_length=100, blank=True, null=True, help_text="Role or user who rejected")

    po_raised = models.BooleanField(default=False, help_text="Marks whether procurement has raised a purchase order for this request")

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
        return self.material_request_id or f"{self.project} - {self.requester_name}"

     
class BOMItem(models.Model):
    material_request = models.ForeignKey(
        MaterialRequest,
        related_name="bom_items",
        on_delete=models.CASCADE
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="material_request_bom_items",
    )

    category = models.CharField(max_length=100, blank=True, null=True)
    specification = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=20, default="pc")

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0
    )

    tax = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0
    )

    inventory_quantity = models.PositiveIntegerField(default=0)

    vendor = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    def __str__(self):
        return f"{self.component} ({self.quantity})"

class RDItem(models.Model):
    material_request = models.ForeignKey(
        MaterialRequest,
        related_name="rd_items",
        on_delete=models.CASCADE
    )

    component = models.ForeignKey(
    "components.Component",
    on_delete=models.CASCADE,
    null=True,
    blank=True
)

    category = models.CharField(max_length=100, blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, default="pc")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    inventory_quantity = models.PositiveIntegerField(default=0)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return str(self.component) if self.component else self.component_code or "RDItem"