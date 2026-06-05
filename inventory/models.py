from django.db import models
from components.models import Component
from procurement.models import PurchaseOrder


class StockIn(models.Model):
    reference_number = models.CharField(max_length=100, unique=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='stock_ins')
    received_by = models.CharField(max_length=100)
    received_date = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.reference_number


class StockInItem(models.Model):
    stock_in = models.ForeignKey(StockIn, on_delete=models.CASCADE, related_name='items')
    component = models.ForeignKey(Component, on_delete=models.CASCADE)
    quantity_received = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)

    @property
    def total_cost(self):
        return self.quantity_received * self.unit_price


class StockOut(models.Model):
    reference_number = models.CharField(max_length=100, unique=True)
    issued_to = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    issued_date = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.reference_number


class StockOutItem(models.Model):
    stock_out = models.ForeignKey(StockOut, on_delete=models.CASCADE, related_name='items')
    component = models.ForeignKey(Component, on_delete=models.CASCADE)
    quantity_issued = models.PositiveIntegerField()


class InventoryLedger(models.Model):
    TRANSACTION_TYPES = [
        ('IN', 'Stock In'),
        ('OUT', 'Stock Out'),
    ]

    component = models.ForeignKey(Component, on_delete=models.CASCADE)
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    quantity = models.IntegerField()
    reference = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.component.name} - {self.transaction_type}"