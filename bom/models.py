from django.db import models
from components.models import Component


class BOM(models.Model):
    bom_number = models.CharField(max_length=100, unique=True)
    bom_name = models.CharField(max_length=100, blank=True, null=True)
    product_name = models.CharField(max_length=255)
    version = models.CharField(max_length=20, default='v1')
    project_type = models.CharField(max_length=50, blank=True, null=True)
    created_by = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.bom_number} - {self.bom_name or self.product_name}"


class BOMItem(models.Model):
    bom = models.ForeignKey(BOM, on_delete=models.CASCADE, related_name="items")
    component = models.ForeignKey(Component, on_delete=models.CASCADE, null=True, blank=True)
    component_code = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=100, blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, default="pc")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    @property
    def total_cost(self):
        return self.quantity * self.price

    def __str__(self):
        return self.component_code or (self.component.name if self.component else "Component")
