from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "inventory",
            "0005_inventory_add_stock_cost_breakdown",
        ),
    ]

    operations = [
        migrations.AlterField(
            model_name="inventory",
            name="received_date",
            field=models.DateField(
                blank=True,
                null=True,
            ),
        ),
    ]
