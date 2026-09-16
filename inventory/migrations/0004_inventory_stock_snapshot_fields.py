from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0003_serial_purchase_costs"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventory",
            name="specifications",
            field=models.TextField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="component_type",
            field=models.CharField(
                blank=True,
                max_length=150,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="uom",
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="unit_price",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
    ]
