from django.db import migrations, models


def backfill_legacy_locations(apps, schema_editor):
    Inventory = apps.get_model("inventory", "Inventory")
    rows = Inventory.objects.using(schema_editor.connection.alias).exclude(
        legacy_source_key__isnull=True,
    ).exclude(legacy_source_key="")
    for row in rows.iterator(chunk_size=200):
        metadata = row.legacy_source_data or {}
        source = metadata.get("original_values", {}) if isinstance(metadata, dict) else {}
        if not isinstance(source, dict):
            continue
        rack = source.get("Rack No")
        box = source.get("Box No")
        row.rack_no = str(rack).strip()[:100] if rack is not None else ""
        row.box_no = str(box).strip()[:100] if box is not None else ""
        if row.rack_no or row.box_no:
            row.save(update_fields=["rack_no", "box_no"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0009_inventory_legacy_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventory",
            name="rack_no",
            field=models.CharField(max_length=100, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="inventory",
            name="box_no",
            field=models.CharField(max_length=100, blank=True, default=""),
        ),
        migrations.RunPython(backfill_legacy_locations, migrations.RunPython.noop),
    ]
