from django.db import models
from components.models import Component


class BOM(models.Model):
    bom_number = models.CharField(max_length=100, unique=True)
    product_name = models.CharField(max_length=255)
    version = models.CharField(max_length=20, default='v1')
    project_type = models.CharField(max_length=50, blank=True, null=True)
    bom_name = models.CharField(max_length=100, blank=True, null=True)
    created_by = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.bom_number} - {self.product_name}"


class BOMItem(models.Model):
    bom = models.ForeignKey(
        BOM,
        on_delete=models.CASCADE,
        related_name="items"
    )

    component_code = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    category = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    specifications = models.TextField(
        blank=True,
        null=True
    )

    quantity = models.PositiveIntegerField(default=1)

    unit = models.CharField(
        max_length=20,
        default="pc"
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

    vendor = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    @property
    def total_cost(self):
        return self.quantity * self.price

    def __str__(self):
        return self.component_code or "Component"
    bom = models.ForeignKey(BOM, on_delete=models.CASCADE, related_name='items')
    category = models.CharField(max_length=100, blank=True, null=True)
    component = models.ForeignKey(Component, on_delete=models.CASCADE)
    specifications = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    quantity = models.PositiveIntegerField()
    vendor = models.CharField(max_length=100, blank=True, null=True)

    @property
    def total_cost(self):
        return self.quantity * self.component.unit_price

    def __str__(self):
        return f"{self.component.name} x {self.quantity}"