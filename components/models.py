from django.db import models


class Component(models.Model):
    CATEGORY_CHOICES = [
        ('ACCESSORIES', 'Accessories'),
        ('AIRFRAMES', 'Airframes'),
        ('COMMUNICATION', 'Communication'),
        ('ELECTRICALS', 'Electricals'),
        ('PAYLOAD', 'Payload'),
        ('TOOLS', 'Tools'),
    ]

    component_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    specifications = models.TextField(blank=True, null=True)
    unit_of_measurements = models.CharField(max_length=100, blank=True, null=True)
    hsn_numbers = models.CharField(max_length=100, blank=True, null=True)
    sku_numbers = models.CharField(max_length=100, blank=True, null=True)
    part_numbers = models.CharField(max_length=100, blank=True, null=True)
    ordering_id = models.IntegerField(blank=True, null=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    stock_quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=5)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_value(self):
        return self.unit_price * self.stock_quantity

    def __str__(self):
        return f"{self.component_id} - {self.name}"
