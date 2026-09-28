from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("components", "0004_alter_component_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="component",
            name="legacy_inventory_id",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
