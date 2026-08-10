from django.db import models


class Inventory(models.Model):
    """
    Central physical In-Store inventory.

    Project-specific allocation and reservation information must not be
    stored in this table. Those quantities are maintained through
    InventoryReservation and ProjectInventory.
    """

    inventory_code = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
    )

    component = models.ForeignKey(
        "components.Component",
        on_delete=models.CASCADE,
        related_name="inventory_items",
    )

    category = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    vendor = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    purchase_order = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    quantity = models.PositiveIntegerField(default=1)

    received_date = models.DateField()

    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    issued = models.BooleanField(default=False)

    # Serial numbers currently available in this physical stock row.
    # For Direct Inward stock these are copied from QC-passed rows.
    # For manually added stock they are generated once and persisted.
    serial_numbers = models.JSONField(
        default=list,
        blank=True,
    )

    # Serial numbers already removed from this stock row. This provides
    # a database audit trail and prevents a serial from being reused.
    issued_serial_numbers = models.JSONField(
        default=list,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [
            "received_date",
            "created_at",
            "id",
        ]

        indexes = [
            models.Index(
                fields=["component", "issued"],
                name="inv_comp_issued_idx",
            ),
        ]

    def __str__(self):
        return self.inventory_code or f"Inventory-{self.pk}"


class InventoryReservation(models.Model):
    """
    Reserves existing In-Store stock for one Material Request.

    Reserved stock remains physically in the Inventory table until it is
    provided by the Inventory team. However, the remaining reserved
    quantity is excluded from the stock available to later Material
    Requests.
    """

    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("PARTIAL", "Partially Issued"),
        ("ISSUED", "Issued"),
        ("RELEASED", "Released"),
        ("CANCELLED", "Cancelled"),
    ]

    material_request = models.ForeignKey(
        "materialrequest.MaterialRequest",
        on_delete=models.PROTECT,
        related_name="inventory_reservations",
    )

    component = models.ForeignKey(
        "components.Component",
        on_delete=models.PROTECT,
        related_name="inventory_reservations",
    )

    # Complete quantity requested by this MR for this component.
    requested_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity protected from current In-Store stock for this MR.
    reserved_store_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity that Procurement must purchase for this MR.
    procurement_shortage_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity already deducted from In Store and provided to this MR.
    issued_store_quantity = models.PositiveIntegerField(
        default=0,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="ACTIVE",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "material_request",
                    "component",
                ],
                name="uniq_inv_res_mr_comp",
            ),
        ]

        indexes = [
            models.Index(
                fields=["component", "status"],
                name="inv_res_comp_stat_idx",
            ),
            models.Index(
                fields=["material_request", "status"],
                name="inv_res_mr_stat_idx",
            ),
        ]

        # Earlier manager approvals receive reservation priority.
        ordering = [
            "created_at",
            "id",
        ]

    @property
    def remaining_reserved_quantity(self):
        """
        Reserved quantity still waiting to be issued from In Store.
        """
        return max(
            int(self.reserved_store_quantity or 0)
            - int(self.issued_store_quantity or 0),
            0,
        )

    @property
    def active_reserved_quantity(self):
        """
        Quantity that must be excluded from availability calculations.
        Released and cancelled reservations do not block stock.
        """
        if self.status in {"RELEASED", "CANCELLED"}:
            return 0

        return self.remaining_reserved_quantity

    @property
    def is_fully_issued(self):
        reserved = int(self.reserved_store_quantity or 0)

        return (
            reserved > 0
            and int(self.issued_store_quantity or 0) >= reserved
        )

    def save(self, *args, **kwargs):
        """
        Keep reservation status synchronized with the source-wise issue
        quantity. RELEASED and CANCELLED are preserved because they are
        explicit workflow decisions.
        """
        if self.status not in {"RELEASED", "CANCELLED"}:
            reserved = int(self.reserved_store_quantity or 0)
            issued = int(self.issued_store_quantity or 0)

            if reserved > 0 and issued >= reserved:
                self.status = "ISSUED"
            elif issued > 0:
                self.status = "PARTIAL"
            else:
                self.status = "ACTIVE"

        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "status",
            }

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.material_request.material_request_id}"
            f" - {self.component}"
        )


class ProjectInventory(models.Model):
    """
    Component quantities allocated to one project through one Material
    Request.

    One row is maintained for each Material Request + Component. The row
    tracks how much is allocated from existing In-Store stock, how much is
    available from QC-passed procurement, and how much has been provided
    from each source.
    """

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("PARTIAL", "Partially Issued"),
        ("READY", "Ready"),
        ("ISSUED", "Issued"),

        # Temporary compatibility for old database rows.
        ("COMPLETED", "Completed - Legacy"),
    ]

    material_request = models.ForeignKey(
        "materialrequest.MaterialRequest",
        on_delete=models.PROTECT,
        related_name="project_inventory_items",
    )

    project = models.CharField(
        max_length=100,
    )

    component = models.ForeignKey(
        "components.Component",
        on_delete=models.PROTECT,
        related_name="project_inventory_items",
    )

    # Complete quantity requested by this MR for this component.
    requested_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity allocated from reserved existing In-Store stock.
    store_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity available from QC-passed procurement.
    purchased_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Cumulative QC result values linked to this MR component.
    qc_passed_quantity = models.PositiveIntegerField(
        default=0,
    )

    qc_failed_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity currently recorded by the existing project-inventory flow.
    # Views may use this as the displayed project quantity.
    quantity = models.PositiveIntegerField(
        default=0,
    )

    # Cumulative total provided to this MR. This is synchronized from the
    # two source-wise issued fields whenever the model is saved.
    issued_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity provided from reserved In-Store stock.
    issued_store_quantity = models.PositiveIntegerField(
        default=0,
    )

    # Quantity provided from QC-passed purchased stock.
    issued_purchased_quantity = models.PositiveIntegerField(
        default=0,
    )

    po_numbers = models.JSONField(
        default=list,
        blank=True,
    )

    inward_codes = models.JSONField(
        default=list,
        blank=True,
    )

    # All QC-passed serials linked to this MR + component.
    purchased_serial_numbers = models.JSONField(
        default=list,
        blank=True,
    )

    # Exact serials provided to this MR from existing In-Store stock.
    issued_store_serials = models.JSONField(
        default=list,
        blank=True,
    )

    # Exact serials provided to this MR from MR-linked QC-passed stock.
    issued_purchased_serials = models.JSONField(
        default=list,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "material_request",
                    "component",
                ],
                name="uniq_proj_inv_mr_comp",
            ),
        ]

        indexes = [
            models.Index(
                fields=["material_request", "status"],
                name="proj_inv_mr_stat_idx",
            ),
            models.Index(
                fields=["component", "status"],
                name="proj_inv_comp_stat_idx",
            ),
        ]

        ordering = [
            "-updated_at",
            "-id",
        ]

    @property
    def total_ready_quantity(self):
        """
        Total quantity allocated and available from both sources.
        """
        return (
            int(self.store_quantity or 0)
            + int(self.purchased_quantity or 0)
        )

    @property
    def calculated_issued_quantity(self):
        """
        Total quantity provided from both sources.
        """
        return (
            int(self.issued_store_quantity or 0)
            + int(self.issued_purchased_quantity or 0)
        )

    @property
    def remaining_store_quantity(self):
        """
        Reserved In-Store quantity still waiting to be provided.
        """
        return max(
            int(self.store_quantity or 0)
            - int(self.issued_store_quantity or 0),
            0,
        )

    @property
    def remaining_purchased_quantity(self):
        """
        QC-passed purchased quantity still waiting to be provided.
        """
        return max(
            int(self.purchased_quantity or 0)
            - int(self.issued_purchased_quantity or 0),
            0,
        )

    @property
    def remaining_quantity(self):
        """
        Complete MR quantity still waiting to be provided.
        """
        return max(
            int(self.requested_quantity or 0)
            - int(self.calculated_issued_quantity or 0),
            0,
        )

    @property
    def is_fulfilled(self):
        requested = int(self.requested_quantity or 0)

        return (
            requested > 0
            and int(self.calculated_issued_quantity or 0) >= requested
        )

    def save(self, *args, **kwargs):
        """
        Synchronize cumulative issue quantity and workflow status.

        PENDING:
            Required quantity is not completely ready and nothing has
            been issued.

        READY:
            The complete requested quantity is available from Store + QC,
            but nothing has been issued.

        PARTIAL:
            Some quantity has been issued, but the request is incomplete.

        ISSUED:
            The complete requested quantity has been issued.
        """
        self.issued_quantity = self.calculated_issued_quantity

        requested = int(self.requested_quantity or 0)
        ready_quantity = int(self.total_ready_quantity or 0)
        issued_quantity = int(self.issued_quantity or 0)

        if requested > 0 and issued_quantity >= requested:
            self.status = "ISSUED"
        elif issued_quantity > 0:
            self.status = "PARTIAL"
        elif requested > 0 and ready_quantity >= requested:
            self.status = "READY"
        else:
            self.status = "PENDING"

        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "issued_quantity",
                "status",
            }

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.material_request.material_request_id}"
            f" - {self.component}"
        )