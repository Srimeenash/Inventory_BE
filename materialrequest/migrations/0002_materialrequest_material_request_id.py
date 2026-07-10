from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("materialrequest", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="materialrequest",
            name="material_request_id",
            field=models.CharField(blank=True, max_length=50, null=True, unique=True),
        ),
    ]
