from django.db import models
from components.models import Component


class BOM(models.Model):
    bom_number = models.CharField(max_length=100, unique=True)
    product_name = models.CharField(max_length=255)
    version = models.CharField(max_length=20, default='v1')

    created_by = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.bom_number} - {self.product_name}"


class BOMItem(models.Model):
    bom = models.ForeignKey(BOM, on_delete=models.CASCADE, related_name='items')
    component = models.ForeignKey(Component, on_delete=models.CASCADE)

    quantity = models.PositiveIntegerField()

    @property
    def total_cost(self):
        return self.quantity * self.component.unit_price

    def __str__(self):
        return f"{self.component.name} x {self.quantity}"