from django.db import models


class DashboardSnapshot(models.Model):
    snapshot_date = models.DateTimeField(auto_now_add=True)

    total_components = models.IntegerField(default=0)
    total_stock_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    pending_pr = models.IntegerField(default=0)
    pending_po = models.IntegerField(default=0)

    pending_approvals = models.IntegerField(default=0)

    total_bom = models.IntegerField(default=0)

    def __str__(self):
        return f"Dashboard Snapshot {self.snapshot_date}"