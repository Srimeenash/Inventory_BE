from decimal import Decimal

from django.db import models
from components.models import Component

STATUS_CHOICES = [
    ("DRAFT", "Draft"),
    ("PENDING", "Pending"),
    ("APPROVED", "Approved"),
    ("ORDERED", "Ordered"),
    ("DELIVERED", "Delivered"),
    ("REJECTED", "Rejected"),
]

class PurchaseRequest(models.Model):

    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("ORDERED", "Ordered"),
        ("DELIVERED", "Delivered"),
        ("REJECTED", "Rejected"),
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
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("ORDERED", "Ordered"),
        ("DELIVERED", "Delivered"),
        ("REJECTED", "Rejected"),
    ]

    approval_status = models.CharField(
    max_length=20,
    choices=[
        ("NOT_REQUESTED", "Not Requested"),
        ("REQUESTED", "Requested"),
        ("MANAGER_APPROVED", "Manager Approved"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ],
    default="NOT_REQUESTED"
)
    po_number = models.CharField(max_length=50, unique=True)

    vendor_name = models.CharField(max_length=255)
    gstin = models.CharField(max_length=30, blank=True)
    location = models.CharField(max_length=255, blank=True)

    ordered_date = models.DateField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)  # ✅ ADD THIS

    remarks = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

    created_at = models.DateTimeField(auto_now_add=True)
    
    rejection_reason = models.TextField(blank=True, null=True, help_text="Reason for rejection")
    
    rejected_by = models.CharField(max_length=100, blank=True, null=True, help_text="Role or user who rejected")

    @property
    def total_quantity(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def grand_subtotal(self):
        return sum(item.subtotal for item in self.items.all())

    @property
    def grand_gst_amount(self):
        return sum(
            (item.gst_amount or Decimal("0"))
            for item in self.items.all()
        )

    @property
    def grand_total(self):
        return sum(
            (item.total_cost or Decimal("0"))
            for item in self.items.all()
        )

    def __str__(self):
        return self.po_number


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items"
    )

    component = models.ForeignKey(Component, on_delete=models.CASCADE)

    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    gst_percentage = models.DecimalField(
    max_digits=5,
    decimal_places=2,
    null=True,
    blank=True
    )

    @property
    def subtotal(self):
        return Decimal(self.quantity) * (self.unit_price or Decimal("0"))

    @property
    def gst_amount(self):
        if self.gst_percentage is None:
            return None

        return (self.subtotal * self.gst_percentage) / Decimal("100")


    @property
    def total_cost(self):
        if self.gst_percentage is None:
            return None

        return self.subtotal + self.gst_amount

class PurchaseOrderApproval(models.Model):
    ACTION_CHOICES = [
        ("REQUESTED", "Requested"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="approvals"
    )

    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        default="REQUESTED"
    )

    requested_by = models.CharField(max_length=100)
    remarks = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.purchase_order.po_number} - {self.action}"