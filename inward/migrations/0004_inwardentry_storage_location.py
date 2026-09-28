from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inward", "0003_inwardentry_qc_failed_action_and_more"),
        ("inventory", "0010_inventory_storage_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="inwardentry",
            name="rack_no",
            field=models.CharField(max_length=100, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="inwardentry",
            name="box_no",
            field=models.CharField(max_length=100, blank=True, default=""),
        ),
    ]
