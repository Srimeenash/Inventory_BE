from pathlib import Path

path = Path('materialrequest/models.py')
text = path.read_text()
before = (
    '    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)\n'
    '    vendor = models.CharField(max_length=255, blank=True, null=True)\n'
    '    remarks = models.TextField(blank=True, null=True)\n'
)
after = (
    '    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)\n'
    '    inventory_quantity = models.PositiveIntegerField(default=0)\n'
    '    vendor = models.CharField(max_length=255, blank=True, null=True)\n'
    '    remarks = models.TextField(blank=True, null=True)\n'
)
if before in text:
    path.write_text(text.replace(before, after, 1))
    print('Patched RDItem inventory_quantity field')
else:
    idx = text.find('total_price = models.DecimalField')
    print('Old block not found')
    print('Index:', idx)
    print(text[idx:idx+300])
