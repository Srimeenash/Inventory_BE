from django.db import models


class ReportLog(models.Model):
    REPORT_TYPES = [
        ('INVENTORY', 'Inventory Report'),
        ('PROCUREMENT', 'Procurement Report'),
        ('FINANCE', 'Finance Report'),
        ('BOM', 'BOM Report'),
    ]

    report_type = models.CharField(max_length=20, choices=REPORT_TYPES)
    generated_by = models.CharField(max_length=100)

    from_date = models.DateField(null=True, blank=True)
    to_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.report_type} - {self.created_at}"