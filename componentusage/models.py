from django.db import models


class ComponentUsage(models.Model):
    STATUS_CHOICES = [
        ("ISSUED", "Issued"),
        ("RETURNED", "Returned"),
        ("PENDING", "Pending"),
    ]

    employee_name = models.CharField(max_length=100)
    component_name = models.CharField(max_length=150)
    component_type = models.CharField(max_length=100)

    requested_date = models.DateField()
    issued_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)

    quantity = models.PositiveIntegerField(default=1)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )

    remarks = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee_name} - {self.component_name}"
    def save(self, *args, **kwargs):
        if self.received_date:
            self.status = "RETURNED"
        elif self.issued_date:
            self.status = "ISSUED"
        else:
            self.status = "PENDING"

        super().save(*args, **kwargs)