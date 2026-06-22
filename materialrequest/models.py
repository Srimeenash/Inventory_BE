# models.py
from django.db import models

class MaterialRequest(models.Model):
    requester_name = models.CharField(max_length=100)
    date = models.DateField()
    project = models.CharField(max_length=100)
    bom = models.CharField(max_length=100)
    required_quantity = models.PositiveIntegerField()
    required_date = models.DateField()
    remarks = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=[("Pending", "Pending"), ("Approved", "Approved"), ("Rejected", "Rejected")],
        default="Pending"
    )

    def __str__(self):
        return f"{self.project} - {self.requester_name}"


class BOMItem(models.Model):
    material_request = models.ForeignKey(
        MaterialRequest, related_name="bom_items", on_delete=models.CASCADE
    )
    specification = models.CharField(max_length=200)
    unit = models.CharField(max_length=50)
    quantity = models.PositiveIntegerField()
    vendor = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.specification} ({self.quantity} {self.unit})"
