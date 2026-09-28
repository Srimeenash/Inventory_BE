from collections import defaultdict

from django.db import migrations


def backfill_legacy_specifications(apps, schema_editor):
    """Fill only empty imported stock specifications from its source row."""
    Inventory = apps.get_model("inventory", "Inventory")
    Component = apps.get_model("components", "Component")
    db = schema_editor.connection.alias
    by_component = defaultdict(set)

    stocks = Inventory.objects.using(db).filter(
        legacy_source_key__startswith="stock-verification:"
    ).only("id", "component_id", "specifications", "legacy_source_data")

    for stock in stocks.iterator(chunk_size=200):
        metadata = stock.legacy_source_data or {}
        values = metadata.get("original_values") if isinstance(metadata, dict) else {}
        if not isinstance(values, dict):
            continue
        specification = str(
            values.get("Component Specification") or values.get("Description") or ""
        ).strip()
        if not specification or specification.upper() in {"-", "N/A"}:
            continue

        if not str(stock.specifications or "").strip():
            Inventory.objects.using(db).filter(pk=stock.pk).update(
                specifications=specification
            )
        if stock.component_id:
            by_component[stock.component_id].add(specification)

    # A component may have multiple stock batches. Do not guess which
    # description belongs in a shared master if its sources disagree.
    for component_id, descriptions in by_component.items():
        if len(descriptions) != 1:
            continue
        component = Component.objects.using(db).filter(pk=component_id).first()
        if component and not str(component.specifications or "").strip():
            Component.objects.using(db).filter(pk=component_id).update(
                specifications=next(iter(descriptions))
            )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0010_inventory_storage_location"),
        ("components", "0005_component_legacy_inventory_id"),
    ]

    operations = [
        migrations.RunPython(backfill_legacy_specifications, migrations.RunPython.noop),
    ]
