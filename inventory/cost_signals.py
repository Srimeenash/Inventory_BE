from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Inventory


@receiver(post_save, sender=Inventory, dispatch_uid='inventory_serial_cost_value')
def inventory_serial_cost_value(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    from .costing import record_inventory_costs, update_stock_value
    if created:
        record_inventory_costs(instance)
        instance.refresh_from_db(fields=["total_price"])
    else:
        update_stock_value(instance)
