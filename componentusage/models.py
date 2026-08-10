from django.db import models


class ComponentUsage(models.Model):
    STATUS_CHOICES = [
        ("ISSUED", "Issued"),
        ("RETURNED", "Returned"),
        ("PENDING", "Pending"),
    ]

    SOURCE_CHOICES = [
        ("INVENTORY", "In-Store Component"),
        ("OTHER", "Other Item"),
    ]

    employee_name = models.CharField(max_length=100)

    # INVENTORY = linked to Component master and physical In-Store stock.
    # OTHER = calculator, notebook, charger, battery pack, etc.
    item_source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default="INVENTORY",
    )

    component = models.ForeignKey(
        "components.Component",
        on_delete=models.PROTECT,
        related_name="usage_records",
        null=True,
        blank=True,
    )

    # Keep these snapshot/text fields for existing records and for OTHER items.
    component_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    # Existing field is retained, but in the UI it is presented as Category.
    component_type = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    requested_date = models.DateField()
    issued_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)

    quantity = models.PositiveIntegerField(default=1)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING",
    )

    remarks = models.TextField(blank=True, null=True)

    # Exact physical Inventory serials actually issued for this usage record.
    # Serial selection is OPTIONAL. If the user does not select serials,
    # the backend chooses FIFO serials and stores the actual issued serials here.
    issued_serial_numbers = models.JSONField(
        default=list,
        blank=True,
    )

    # Audit trail of the physical Inventory rows used for this issue.
    # Example:
    # [
    #   {
    #       "inventory_id": 12,
    #       "inventory_code": "INV00012",
    #       "quantity": 2,
    #       "serial_numbers": ["...", "..."]
    #   }
    # ]
    inventory_issue_details = models.JSONField(
        default=list,
        blank=True,
    )

    # True only after physical In-Store stock has actually been deducted.
    inventory_adjusted = models.BooleanField(default=False)

    # True after the same physical stock has been restored on RETURNED.
    inventory_returned = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.employee_name} - {self.component_name}"

    def save(self, *args, **kwargs):
        if self.component_id and self.item_source == "INVENTORY":
            # Snapshot the master values so the usage history remains readable.
            self.component_name = self.component.name
            self.component_type = self.component.category

        if self.received_date:
            self.status = "RETURNED"
        elif self.issued_date:
            self.status = "ISSUED"
        else:
            self.status = "PENDING"

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "status",
                "component_name",
                "component_type",
            }

        super().save(*args, **kwargs)