from decimal import Decimal

from django.db import models
from components.models import Component


class PurchaseRequest(models.Model):

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("CONVERTED", "Converted To PO"),
    ]

    pr_number = models.CharField(
        max_length=50,
        unique=True
    )

    requested_by = models.CharField(
        max_length=100
    )

    department = models.CharField(
        max_length=100
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.pr_number


class PurchaseRequestItem(models.Model):

    purchase_request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.CASCADE,
        related_name="items"
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE
    )

    quantity = models.PositiveIntegerField()

    remarks = models.TextField(
        blank=True,
        null=True
    )

    def __str__(self):
        return f"{self.component} - {self.quantity}"


class PurchaseOrder(models.Model):

    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("ORDERED", "Ordered"),
        ("PARTIAL", "Partially Received"),
        ("RECEIVED", "Received"),
        ("CANCELLED", "Cancelled"),
    ]

    po_number = models.CharField(
        max_length=50,
        unique=True
    )

    purchase_request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders"
    )

    # Vendor Details
    vendor_name = models.CharField(
        max_length=255
    )

    gstin = models.CharField(
        max_length=30,
        blank=True
    )

    location = models.CharField(
        max_length=255,
        blank=True
    )

    ordered_date = models.DateField(
        null=True,
        blank=True
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="DRAFT"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    @property
    def total_quantity(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def grand_subtotal(self):
        return sum(item.subtotal for item in self.items.all())

    @property
    def grand_gst_amount(self):
        return sum(item.gst_amount for item in self.items.all())

    @property
    def grand_total(self):
        return sum(item.total_cost for item in self.items.all())

    def __str__(self):
        return self.po_number


class PurchaseOrderItem(models.Model):

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items"
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE
    )

    quantity = models.PositiveIntegerField()

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    gst_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=18.00
    )

    # Logistics / Inward
    shipping_qty = models.PositiveIntegerField(
        default=0
    )

    shipping_date = models.DateField(
        null=True,
        blank=True
    )

    received_qty = models.PositiveIntegerField(
        default=0
    )

    received_date = models.DateField(
        null=True,
        blank=True
    )

    inwarded = models.BooleanField(
        default=False
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    @property
    def specification(self):
        return getattr(
            self.component,
            "component_specification",
            ""
        )

    @property
    def uom(self):
        return getattr(
            self.component,
            "unit_of_measurement",
            ""
        )

    @property
    def subtotal(self):
        return Decimal(self.quantity) * self.unit_price

    @property
    def gst_amount(self):
        return (
            self.subtotal * self.gst_percentage
        ) / Decimal("100")

    @property
    def total_cost(self):
        return self.subtotal + self.gst_amount

    def __str__(self):
        return (
            f"{self.purchase_order.po_number} - "
            f"{self.component}"
        )
        