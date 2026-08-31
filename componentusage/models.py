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
    PURPOSE_CHOICES = [
        ("FLIGHT_TEST", "Flight Test"),
        ("CUSTOMER_DEMO", "Customer Demo"),
        ("QC_CHECK", "QC Check"),
        ("EVENT", "Event"),
        ("MISCELLANEOUS_USAGE", "Miscellaneous Usage"),
    ]
    RETURN_CONDITION_CHOICES = [
        ("", "Not Checked"),
        ("OK", "OK"),
        ("NOT_OK", "Not OK"),
    ]
    RETURN_APPROVAL_CHOICES = [
        ("NOT_REQUIRED", "Not Required"),
        ("PENDING_MANAGER", "Pending Manager"),
        ("PENDING_FINANCE", "Pending Finance"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("COMPLETED", "Completed"),
    ]

    material_request = models.ForeignKey(
        "materialrequest.MaterialRequest",
        on_delete=models.CASCADE,
        related_name="returnable_records",
        null=True,
        blank=True,
        db_index=True,
    )
    employee_name = models.CharField(max_length=100)
    item_source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="INVENTORY")
    component = models.ForeignKey(
        "components.Component",
        on_delete=models.PROTECT,
        related_name="usage_records",
        null=True,
        blank=True,
    )
    component_name = models.CharField(max_length=150, blank=True, default="")
    component_type = models.CharField(max_length=100, blank=True, default="")
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, blank=True, default="", db_index=True)

    requested_date = models.DateField()
    return_due_date = models.DateField(null=True, blank=True)
    issued_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    remarks = models.TextField(blank=True, null=True)

    return_condition = models.CharField(
        max_length=10,
        choices=RETURN_CONDITION_CHOICES,
        blank=True,
        default="",
        db_index=True,
    )
    return_reason = models.TextField(blank=True, default="")
    return_approval_status = models.CharField(
        max_length=30,
        choices=RETURN_APPROVAL_CHOICES,
        default="NOT_REQUIRED",
        db_index=True,
    )

    issued_serial_numbers = models.JSONField(default=list, blank=True)
    inventory_issue_details = models.JSONField(default=list, blank=True)
    inventory_adjusted = models.BooleanField(default=False)
    inventory_returned = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["purpose", "status"], name="cu_purpose_status_idx"),
            models.Index(fields=["return_condition", "return_approval_status"], name="cu_return_flow_idx"),
        ]

    def __str__(self):
        reference = (
            getattr(self.material_request, "material_request_id", "")
            if self.material_request_id
            else ""
        )
        return f"{reference or self.employee_name} - {self.component_name}"

    def save(self, *args, **kwargs):
        if self.component_id and self.item_source == "INVENTORY":
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
