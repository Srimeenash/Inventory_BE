from django.db import models
from components.models import Component

class Inventory(models.Model):
    inventory_code = models.CharField(max_length=100, unique=True, blank=True)
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="inventory_items",
    )
    category = models.CharField(max_length=100, blank=True, null=True)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    purchase_order = models.CharField(max_length=255, blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    received_date = models.DateField()
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    issued = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.inventory_code
