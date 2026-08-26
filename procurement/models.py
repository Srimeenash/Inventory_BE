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

    pr_number = models.CharField(max_length=50, unique=True)
    requested_by = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    remarks = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.pr_number


class PurchaseRequestItem(models.Model):
    purchase_request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.CASCADE,
        related_name="items",
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField()
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.component} - {self.quantity}"


class PurchaseOrder(models.Model):
    ORDER_TYPE_CHOICES = [
        ("STANDARD", "Standard Purchase Order"),
        ("REPLACEMENT", "QC Replacement Order"),
    ]

    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("PENDING", "Pending"),
        ("PENDING_FINANCE", "Pending Finance"),
        ("FINANCE_APPROVED", "Finance Approved"),
        ("FINANCE_REJECTED", "Finance Rejected"),
        ("APPROVED", "Approved"),
        ("ORDERED", "Ordered"),
        ("PARTIALLY_DELIVERED", "Partially Delivered"),
        ("DELIVERED", "Delivered"),
        ("REJECTED", "Rejected"),
        # QC replacement cycle. Normal PO statuses above are unchanged.
        (
            "REPLACEMENT_PENDING_MANAGER",
            "Replacement - Pending Manager Approval",
        ),
        (
            "REPLACEMENT_PENDING_FINANCE",
            "Replacement - Pending Finance Approval",
        ),
        (
            "REPLACEMENT_MANAGER_REJECTED",
            "Replacement - Manager Rejected",
        ),
        (
            "REPLACEMENT_FINANCE_REJECTED",
            "Replacement - Finance Rejected",
        ),
        ("REPLACEMENT_APPROVED", "Replacement Approved"),
        ("REPLACEMENT_ORDERED", "Replacement Ordered"),
        (
            "REPLACEMENT_PARTIALLY_RECEIVED",
            "Replacement Partially Received",
        ),
        ("REPLACEMENT_RECEIVED", "Replacement Received"),
    ]

    APPROVAL_STATUS_CHOICES = [
        ("NOT_REQUESTED", "Not Requested"),
        ("PENDING", "Pending"),
        ("PENDING_FINANCE", "Pending Finance"),
        ("FINANCE_APPROVED", "Finance Approved"),
        ("FINANCE_REJECTED", "Finance Rejected"),
        # Replacement-specific approval states.
        (
            "REPLACEMENT_PENDING_MANAGER",
            "Replacement - Pending Manager Approval",
        ),
        (
            "REPLACEMENT_MANAGER_APPROVED",
            "Replacement - Manager Approved",
        ),
        (
            "REPLACEMENT_MANAGER_REJECTED",
            "Replacement - Manager Rejected",
        ),
        (
            "REPLACEMENT_PENDING_FINANCE",
            "Replacement - Pending Finance Approval",
        ),
        (
            "REPLACEMENT_FINANCE_APPROVED",
            "Replacement - Finance Approved",
        ),
        (
            "REPLACEMENT_FINANCE_REJECTED",
            "Replacement - Finance Rejected",
        ),
    ]

    approval_status = models.CharField(
        max_length=40,
        choices=APPROVAL_STATUS_CHOICES,
        default="PENDING",
    )

    po_number = models.CharField(max_length=80, unique=True)
    vendor_name = models.CharField(max_length=255)
    gstin = models.CharField(max_length=30, blank=True)
    location = models.CharField(max_length=255, blank=True)
    ordered_date = models.DateField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)
    finance_remarks = models.TextField(
        blank=True,
        null=True,
        help_text="Finance approval/rejection remarks",
    )
    status = models.CharField(
        max_length=40,
        choices=STATUS_CHOICES,
        default="PENDING",
    )
    rejection_reason = models.TextField(blank=True, null=True)
    source_mr_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Material Request number when this PO was created from an MR",
    )
    rejected_by = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    approved_at = models.DateTimeField(blank=True, null=True)

    # ---------------------------------------------------------
    # QC replacement audit/link fields.
    # ---------------------------------------------------------
    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPE_CHOICES,
        default="STANDARD",
        db_index=True,
    )
    replacement_for = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replacement_orders",
        help_text="Original standard PO that this QC replacement belongs to.",
    )
    replacement_round = models.PositiveIntegerField(
        default=0,
        help_text="Replacement sequence number: R1, R2, R3...",
    )
    replacement_source_inward_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="InwardEntry ID whose QC failure created this replacement order.",
    )

    @property
    def is_replacement(self):
        return str(self.order_type or "").upper() == "REPLACEMENT"

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
        related_name="items",
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField()
    received_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Total quantity received through inward entries",
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    gst_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )

    # Each component in one Purchase Order can have its own delivery date.
    # This is especially required when Procurement combines multiple
    # same-vendor MR components into a single PO.
    expected_delivery_date = models.DateField(
        null=True,
        blank=True,
    )

    @property
    def remaining_quantity(self):
        return max(self.quantity - self.received_quantity, 0)

    @property
    def is_fully_received(self):
        return self.received_quantity >= self.quantity

    @property
    def subtotal(self):
        return Decimal(self.quantity) * (
            self.unit_price or Decimal("0")
        )

    @property
    def gst_amount(self):
        if self.gst_percentage is None:
            return None
        return (
            self.subtotal * self.gst_percentage
        ) / Decimal("100")

    @property
    def total_cost(self):
        if self.gst_percentage is None:
            return None
        return self.subtotal + self.gst_amount


class PurchaseOrderApproval(models.Model):
    ACTION_CHOICES = [
        ("REQUESTED", "Requested"),
        ("PENDING_FINANCE", "Pending Finance"),
        ("FINANCE_APPROVED", "Finance Approved"),
        ("FINANCE_REJECTED", "Finance Rejected"),
        # Replacement audit history.
        ("REPLACEMENT_REQUESTED", "Replacement Requested"),
        (
            "REPLACEMENT_MANAGER_APPROVED",
            "Replacement Manager Approved",
        ),
        (
            "REPLACEMENT_MANAGER_REJECTED",
            "Replacement Manager Rejected",
        ),
        (
            "REPLACEMENT_FINANCE_APPROVED",
            "Replacement Finance Approved",
        ),
        (
            "REPLACEMENT_FINANCE_REJECTED",
            "Replacement Finance Rejected",
        ),
        ("REPLACEMENT_ORDERED", "Replacement Ordered"),
    ]

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    action = models.CharField(
        max_length=40,
        choices=ACTION_CHOICES,
        default="REQUESTED",
    )
    requested_by = models.CharField(max_length=100)
    approved_by = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    finance_remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.purchase_order.po_number} - {self.action}"