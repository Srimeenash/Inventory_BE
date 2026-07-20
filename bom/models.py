from django.db import models
from components.models import Component


class BOM(models.Model):
    bom_number = models.CharField(max_length=100, unique=True)
    bom_name = models.CharField(max_length=100, blank=True, null=True)
    product_name = models.CharField(max_length=255)
    version = models.CharField(max_length=20, default='v1')
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
    vendor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)


    def __str__(self):
        return self.component_code or (self.component.name if self.component else "Component")
