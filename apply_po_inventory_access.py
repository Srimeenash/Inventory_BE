"""Give the Inventory role Purchase Order work access in an existing IPMS checkout.

Place this file in BE beside manage.py and run it once with PowerShell:
    python .\apply_po_inventory_access.py

The script checks every expected code location before changing any file and
keeps an individual .bak copy of each original file.
"""

from pathlib import Path
import os
import shutil
import sys


def replace_once(source, old, new, label):
    if source.count(old) != 1:
        raise ValueError(f"{label}: expected exactly one matching code block; no files changed.")
    return source.replace(old, new, 1)


def patch_permissions(source):
    old = '  inventory: [\n    "inventory",\n'
    new = '  inventory: [\n    "purchase-order",\n    "inventory",\n'
    if new in source:
        return source
    return replace_once(source, old, new, "Frontend permissions")


def patch_po_page(source):
    old = '  // Only Admin + Procurement can CREATE / DELETE / CHANGE PO data.\n'
    new = '  // Admin, Procurement and Inventory can work on Purchase Orders.\n'
    if new not in source:
        source = replace_once(source, old, new, "Purchase Order page")

    old = '''  const isProcurementRole =
    activeRole === "procurement" ||
    activeRole === "admin";
'''
    new = '''  const isProcurementRole =
    activeRole === "procurement" ||
    activeRole === "inventory" ||
    activeRole === "admin";
'''
    if new not in source:
        source = replace_once(source, old, new, "Replacement PO approval button")
    return source


def patch_po_backend(source):
    old = '''    def replacement_procurement_approve(self, request, pk=None):
        """
        Procurement approves a newly raised QC Replacement PO.

        No Manager/Finance email or notification is created here.
        """
        self.require_active_role(
            request,
            "procurement",
            "admin",
        )
'''
    new = '''    def replacement_procurement_approve(self, request, pk=None):
        """
        Procurement or Inventory approves a newly raised QC Replacement PO.

        No Manager/Finance email or notification is created here.
        """
        self.require_active_role(
            request,
            "procurement",
            "inventory",
            "admin",
        )
'''
    if new not in source:
        source = replace_once(source, old, new, "Replacement PO work approval")

    permission_block = '''        """

        old_approval_status = str(
            serializer.instance.approval_status
'''
    gated_block = '''        """

        # Ordering or recording delivery is PO work, not a Finance/Manager
        # approval. Enforce the active JWT role for these transitions.
        requested_status = str(
            serializer.validated_data.get("status") or ""
        ).strip().upper()
        previous_status = str(
            serializer.instance.status or ""
        ).strip().upper()
        if (
            requested_status in {
                "ORDERED",
                "PARTIALLY_DELIVERED",
                "DELIVERED",
            }
            and requested_status != previous_status
        ):
            self.require_active_role(
                self.request, "procurement", "inventory", "admin"
            )

        old_approval_status = str(
            serializer.instance.approval_status
'''
    if gated_block not in source:
        source = replace_once(source, permission_block, gated_block, "Standard PO transitions")

    old = '''    def replacement_mark_ordered(self, request, pk=None):
        self.require_active_role(request, "procurement", "admin")
'''
    new = '''    def replacement_mark_ordered(self, request, pk=None):
        self.require_active_role(request, "procurement", "inventory", "admin")
'''
    if new not in source:
        source = replace_once(source, old, new, "Replacement PO ordering")

    old = '''        recalculated across every PO belonging to the same MR.
        """

        try:
            purchase_order = (
'''
    new = '''        recalculated across every PO belonging to the same MR.
        """

        self.require_active_role(request, "procurement", "inventory", "admin")

        try:
            purchase_order = (
'''
    if new not in source:
        source = replace_once(source, old, new, "PO delivery receipt")
    return source


def main():
    backend = Path(__file__).resolve().parent
    if not (backend / "manage.py").is_file():
        raise ValueError("Put this script in your BE folder beside manage.py first.")
    frontend = backend.parent / "FE" / "dream-to-life-app" / "src"
    files = [
        (frontend / "permissions.js", patch_permissions),
        (frontend / "routes" / "purchase-orders.jsx", patch_po_page),
        (backend / "procurement" / "views.py", patch_po_backend),
    ]

    # Validate every file before touching any of them.
    changes = []
    for path, patch in files:
        if not path.is_file():
            raise ValueError(f"File not found: {path}. No files changed.")
        raw = path.read_bytes()
        newline = "\r\n" if b"\r\n" in raw else "\n"
        source = raw.decode("utf-8").replace("\r\n", "\n")
        updated = patch(source)
        if path.name == "views.py":
            compile(updated, str(path), "exec")
        if updated != source:
            backup = path.with_name(path.name + ".before_inventory_po_access.bak")
            if backup.exists():
                raise ValueError(f"Backup already exists: {backup}. No files changed.")
            changes.append((path, backup, updated.replace("\n", newline).encode("utf-8")))

    if not changes:
        print("Inventory Purchase Order access is already installed; no files changed.")
        return

    applied = []
    try:
        for path, backup, content in changes:
            shutil.copy2(path, backup)
            pending = path.with_name(path.name + ".inventory_po_access.tmp")
            try:
                pending.write_bytes(content)
                os.replace(pending, path)
            finally:
                if pending.exists():
                    pending.unlink()
            applied.append((path, backup))
            print(f"Updated: {path}\nBackup:  {backup}")
    except OSError:
        for path, backup in reversed(applied):
            shutil.copy2(backup, path)
        raise
    print("Restart Django and Vite, then sign in again with the Inventory role.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeError, ValueError, SyntaxError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        sys.exit(1)
