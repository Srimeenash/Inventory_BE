from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "inventory",
            "0004_inventory_stock_snapshot_fields",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="inventory",
            name="discount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="gst_percentage",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=7,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="gst_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="freight_cost",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="freight_gst_percentage",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=7,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="freight_gst_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="inventory",
            name="round_off",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
            ),
        ),
    ]
