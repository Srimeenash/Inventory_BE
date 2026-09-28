from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0008_merge_20260916_1137"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventory",
            name="legacy_source_key",
            field=models.CharField(blank=True, max_length=120, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="inventory",
            name="legacy_source_data",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
