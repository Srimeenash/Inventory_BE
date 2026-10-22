"""
Safe one-time updater for procurement/models.py, based on the PO model you shared.
Run from the Django backend directory: python apply_po_model_fix.py
Does not modify PurchaseRequest or unrelated model choices.
"""
from pathlib import Path
import ast
import re
import shutil
from datetime import datetime

path = Path("procurement/models.py")
if not path.is_file():
    raise SystemExit("ERROR: Run from your Django backend directory (must contain procurement/models.py)")
source = path.read_text(encoding="utf-8")
start = source.find("class PurchaseOrder(models.Model):")
end = source.find("class PurchaseOrderItem(models.Model):", start)
if start < 0 or end < 0:
    raise SystemExit("ERROR: Expected PurchaseOrder and PurchaseOrderItem classes were not found. No files modified.")
block = source[start:end]
marker = '"MANAGER_REJECTED", "Manager Rejected"'
if marker in block:
    if block.count(marker) == 2:
        print("PurchaseOrder model already includes both MANAGER_REJECTED choices; no changes needed.")
        raise SystemExit(0)
    raise SystemExit("ERROR: Only one of two model choices exists. Update manually; no files modified.")
# Must match exactly one insertion in PO status choices, one in PO approval choices.
pattern = re.compile(r'(?m)^(?P<indent>[ \t]*)\("FINANCE_REJECTED",[ \t]*"Finance Rejected"\),[ \t]*$')
found = list(pattern.finditer(block))
if len(found) != 2:
    raise SystemExit(f"ERROR: Expected two Finance Rejected choice entries in PurchaseOrder; found {len(found)}. No files modified.")
modified = pattern.sub(lambda m: f'{m.group("indent")}("MANAGER_REJECTED", "Manager Rejected"),\n{m.group(0)}', block)
new_source = source[:start] + modified + source[end:]
try:
    ast.parse(new_source, filename=str(path))
except SyntaxError as exc:
    raise SystemExit(f"ERROR: Result would be invalid Python: {exc}. No files modified.")
backup = path.with_name(f"models.py.before_manager_reject_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
shutil.copy2(path, backup)
path.write_text(new_source,encoding="utf-8")
print("Updated:", path)
print("Backup:", backup)
print("Next: run python manage.py makemigrations procurement then python manage.py migrate")
