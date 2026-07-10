from django.db import models
from components.models import Component


class OutwardEntry(models.Model):
    OUTWARD_TYPE_CHOICES = [
        ('SCRAP', 'Scrap'),
        ('SALES', 'Sales'),
        ('EVENT', 'Event'),
    ]

    code = models.CharField(max_length=100, unique=True)
    outward_type = models.CharField(max_length=20, choices=OUTWARD_TYPE_CHOICES, default='SCRAP')
    out_date = models.DateField()
    time = models.TimeField(blank=True, null=True)
    product_name = models.CharField(max_length=255, blank=True, null=True)
    invoice_number = models.CharField(max_length=100, blank=True, null=True)
    client = models.CharField(max_length=255, blank=True, null=True)
    deliverables = models.TextField(blank=True, null=True)
    gate_pass = models.CharField(max_length=100, blank=True, null=True)
    event_name = models.CharField(max_length=255, blank=True, null=True)
    no_of_components = models.PositiveIntegerField(blank=True, null=True)
    return_date = models.DateField(blank=True, null=True)
    drone_name = models.CharField(max_length=255, blank=True, null=True)
    attendee_name = models.CharField(max_length=255, blank=True, null=True)
    event_components = models.TextField(blank=True, null=True)
    is_returned = models.BooleanField(default=False)
    remarks = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    component = models.ForeignKey(Component, on_delete=models.PROTECT, blank=True, null=True, related_name='outward_entries')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.code
