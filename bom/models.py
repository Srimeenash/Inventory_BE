from django.db import models
from components.models import Component


class BOM(models.Model):
    STATUS_CHOICES = [
        ("PENDING_MANAGER", "Pending Manager"),
        ("APPROVED", "Approved"),
        ("MANAGER_REJECTED", "Manager Rejected"),
        ("MODIFIED", "Modified"),
    ]

    bom_number = models.CharField(max_length=100, unique=True)
    bom_name = models.CharField(max_length=100, blank=True, null=True)
    product_name = models.CharField(max_length=255)
    version = models.CharField(max_length=20, default="v1")
    created_by = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default="PENDING_MANAGER",
    )
    manager_rejection_reason = models.TextField(blank=True, null=True)
    manager_rejected_by = models.CharField(max_length=100, blank=True, null=True)
    manager_rejected_at = models.DateTimeField(blank=True, null=True)
    manager_approved_by = models.CharField(max_length=100, blank=True, null=True)
    manager_approved_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.bom_number} - {self.bom_name or self.product_name}"


class BOMItem(models.Model):
    bom = models.ForeignKey(
        BOM,
        on_delete=models.CASCADE,
        related_name="items",
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    component_code = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=100, blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)

    # User-entered BOM UOM. Do not auto-copy Component Master UOM.
    unit = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return (
            self.component_code
            or (self.component.name if self.component else "Component")
        )


class ProjectBOM(models.Model):
    """Immutable per-MR snapshot, including rows removed from the MR payload."""

    material_request = models.OneToOneField(
        "materialrequest.MaterialRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_bom_snapshot",
    )
    material_request_number = models.CharField(max_length=50, unique=True)
    status_snapshot = models.CharField(max_length=40, blank=True, default="")
    approval_status_snapshot = models.CharField(max_length=30, blank=True, default="")
    source_bom = models.ForeignKey(
        BOM,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_snapshots",
    )
    source_bom_number = models.CharField(max_length=100)
    bom_name = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255, blank=True, default="")
    version = models.CharField(max_length=20, blank=True, default="")
    project = models.CharField(max_length=100)
    created_by = models.CharField(max_length=100)
    items_snapshot = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")

    @property
    def project_bom_number(self):
        return f"PBOM-{self.pk:05d}"

    def __str__(self):
        return f"{self.project_bom_number} - {self.bom_name}"


class MasterBOMPricing(models.Model):
    """One editable costing row per component shared by approved Product BOMs."""

    component_code = models.CharField(max_length=100, unique=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=100, blank=True, default="")
    vendor = models.CharField(max_length=255, blank=True, default="")
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    freight_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    freight_gst_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.component_code
