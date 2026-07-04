from django.db import models
from vendors.models import Vendor
from components.models import Component
from procurement.models import PurchaseOrder


class InwardEntry(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PASS', 'Pass'),
        ('FAIL', 'Fail'),
        ('COMPLETED', 'Completed'),
    ]

    code = models.CharField(max_length=100, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='inward_entries')
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='inward_entries')
    component = models.ForeignKey(Component, on_delete=models.PROTECT, related_name='inward_entries')
    quantity_received = models.PositiveIntegerField()
    batch_number = models.CharField(max_length=100, blank=True, null=True)
    received_date = models.DateField()
    qc_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    qc_passed_rows = models.JSONField(default=list, blank=True)
    qc_failed_rows = models.JSONField(default=list, blank=True)
    qc_timestamp = models.DateTimeField(blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.code


class InwardLineItem(models.Model):
    inward_entry = models.ForeignKey(InwardEntry, on_delete=models.CASCADE, related_name='line_items')
    specification = models.CharField(max_length=255, blank=True, null=True)
    invoice_number = models.CharField(max_length=100, blank=True, null=True)
    invoice_date = models.DateField(blank=True, null=True)
    total_quantity = models.PositiveIntegerField(blank=True, null=True)
    quantity = models.PositiveIntegerField(blank=True, null=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    grand_total = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)

    def __str__(self):
        return f"{self.inward_entry.code} - {self.specification or 'Line'}"
