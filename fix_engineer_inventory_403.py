"""Fix the Engineer In-Store endpoint's role check.

Place this file beside manage.py in the BE folder and run:
    python .\fix_engineer_inventory_403.py

It edits only inventory/views.py and saves a timestamped backup first.
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import re
import shutil


ROLE_HELPER = '''\n\ndef _request_user_roles(request):
    """Return roles currently assigned to the authenticated account."""
    user = getattr(request, "user", None)
    get_all_roles = getattr(user, "get_all_roles", None)
    if callable(get_all_roles):
        try:
            roles = get_all_roles()
        except Exception:
            roles = []
    else:
        roles = [getattr(user, "role", "")]

    if not isinstance(roles, (list, tuple, set)):
        roles = [roles]
    additional_roles = getattr(user, "additional_roles", []) or []
    if isinstance(additional_roles, (list, tuple, set)):
        roles = [*roles, *additional_roles]

    return {
        str(getattr(role, "name", role) or "").strip().lower()
        for role in roles
        if str(getattr(role, "name", role) or "").strip()
    }


def _request_has_engineer_role(request):
    return "engineer" in _request_user_roles(request)
'''


SERIALIZER_METHOD = '''    def get_serializer_class(self):
        action_name = getattr(self, "action", "")
        if action_name == "engineer_store" or (
            _request_active_role_name(self.request) == "engineer"
            and action_name in {"list", "retrieve"}
        ):
            return EngineerInventorySerializer
        return super().get_serializer_class()
'''


def patch_source(source: str) -> str:
    if "class InventoryViewSet" not in source:
        raise ValueError("InventoryViewSet was not found in inventory/views.py.")
    if "def engineer_store(self, request):" not in source:
        raise ValueError("The engineer-store API action is missing. No files were changed.")
    if "EngineerInventorySerializer" not in source:
        raise ValueError("EngineerInventorySerializer is missing. No files were changed.")
    if "def _request_active_role_name(request):" not in source:
        raise ValueError("The active-role helper is missing. No files were changed.")

    if "def _request_has_engineer_role(request):" not in source:
        class_marker = "class InventoryViewSet"
        source = source.replace(class_marker, ROLE_HELPER + "\n\n" + class_marker, 1)

    action_start = source.find("    def engineer_store(self, request):")
    action_end = source.find("\n    @action(", action_start)
    if action_end < 0:
        action_end = source.find("\n    def ", action_start + 1)
    action_block = source[action_start:action_end]
    old_guard = 'if _request_active_role_name(request) != "engineer":'
    new_guard = "if not _request_has_engineer_role(request):"
    if old_guard in action_block:
        action_block = action_block.replace(old_guard, new_guard, 1)
        action_block = action_block.replace(
            '"This In-Store view is available to Engineer users only."',
            '"Assign the Engineer role to this account to view In-Store stock."',
            1,
        )
        source = source[:action_start] + action_block + source[action_end:]
    elif new_guard not in action_block:
        raise ValueError("The engineer-store role guard has an unexpected form. No files were changed.")

    class_start = source.find("class InventoryViewSet")
    class_end = source.find("\nclass ProjectInventoryViewSet", class_start)
    if class_end < 0:
        class_end = len(source)
    viewset = source[class_start:class_end]

    serializer_pattern = re.compile(
        r"(?ms)^    def get_serializer_class\(self\):.*?(?=^    def |\Z)"
    )
    serializer_match = serializer_pattern.search(viewset)
    if not serializer_match:
        raise ValueError("InventoryViewSet.get_serializer_class() was not found. No files were changed.")
    viewset = (
        viewset[:serializer_match.start()]
        + SERIALIZER_METHOD
        + viewset[serializer_match.end():]
    )

    queryset_pattern = re.compile(
        r"(?ms)^    def get_queryset\(self\):.*?(?=^    @action\(|^    def |\Z)"
    )
    queryset_match = queryset_pattern.search(viewset)
    if not queryset_match:
        raise ValueError("InventoryViewSet.get_queryset() was not found. No files were changed.")
    queryset_block = queryset_match.group(0)
    old_filter = 'if _request_active_role_name(self.request) == "engineer":'
    new_filter = (
        'if (\n'
        '            getattr(self, "action", "") == "engineer_store"\n'
        '            or _request_active_role_name(self.request) == "engineer"\n'
        '        ):'
    )
    if old_filter in queryset_block:
        queryset_block = queryset_block.replace(old_filter, new_filter, 1)
    elif 'getattr(self, "action", "") == "engineer_store"' not in queryset_block:
        raise ValueError("The Engineer stock filter has an unexpected form. No files were changed.")
    viewset = (
        viewset[:queryset_match.start()]
        + queryset_block
        + viewset[queryset_match.end():]
    )

    return source[:class_start] + viewset + source[class_end:]


def main() -> int:
    backend_root = Path(__file__).resolve().parent
    if not (backend_root / "manage.py").is_file():
        print("Place this script directly in BE, beside manage.py.")
        return 1

    views_path = backend_root / "inventory" / "views.py"
    if not views_path.is_file():
        print(f"Cannot find {views_path}. No files were changed.")
        return 1

    raw = views_path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    source = raw.decode("utf-8-sig").replace("\r\n", "\n")
    try:
        updated = patch_source(source)
        ast.parse(updated)
    except (SyntaxError, ValueError) as error:
        print(f"No files were changed: {error}")
        return 1

    if updated == source:
        print("Engineer In-Store 403 fix is already installed. No files changed.")
        return 0

    backup = views_path.with_name(
        "views_before_engineer_inventory_403_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".py.bak"
    )
    if backup.exists():
        print(f"Backup already exists: {backup}. No files were changed.")
        return 1

    shutil.copy2(views_path, backup)
    views_path.write_bytes(updated.replace("\n", newline).encode("utf-8"))
    print(f"Updated: {views_path}")
    print(f"Backup:  {backup}")
    print("Restart Django, sign out and back in as Engineer, then refresh Inventory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
