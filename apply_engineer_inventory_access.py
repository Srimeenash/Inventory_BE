"""Install the read-only Engineer In-Store Inventory view.

Copy this script and EngineerInventoryPage.jsx beside manage.py in BE, then run:
    python apply_engineer_inventory_access.py

It updates the matching FE and BE files under the usual INVENTORY/FE and
INVENTORY/BE folders. Backups are created before any file is changed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import sys


ENGINEER_SERIALIZER = '''\n\nclass EngineerInventorySerializer(serializers.ModelSerializer):
    """Minimal In-Store fields available to Engineer users."""

    component_id = serializers.SerializerMethodField()
    specification = serializers.SerializerMethodField()

    class Meta:
        model = Inventory
        fields = (
            "id",
            "component_id",
            "specification",
            "quantity",
            "uom",
        )
        read_only_fields = fields

    def get_component_id(self, obj):
        inventory_serializer = InventorySerializer()
        if obj.legacy_source_key:
            # Preserve the workbook ID. Missing source IDs remain N/A; do not
            # replace them with a category-generated component ID.
            return inventory_serializer.get_source_component_id(obj) or "N/A"

        legacy_id = str(
            getattr(obj.component, "legacy_inventory_id", "") or ""
        ).strip()
        return legacy_id or str(
            getattr(obj.component, "component_id", "") or "N/A"
        ).strip()

    def get_specification(self, obj):
        value = str(obj.specifications or "").strip()
        if value in {"", "-", "—"}:
            value = InventorySerializer().get_source_specification(obj)
        if value in {"", "-", "—"} and obj.component_id:
            value = str(
                getattr(obj.component, "specifications", "") or ""
            ).strip()
        return value or "—"
'''


ACTIVE_ROLE_HELPER = '''\n\ndef _request_active_role_name(request):
    """Return the role selected for this authenticated session."""
    auth = getattr(request, "auth", None)
    role = ""
    if auth is not None:
        try:
            role = auth.get("active_role") or auth.get("activeRole") or ""
        except (AttributeError, TypeError):
            role = ""
    user = getattr(request, "user", None)
    role = role or getattr(user, "active_role", "") or getattr(user, "activeRole", "")
    if not role:
        role = getattr(user, "role", "")
    role = getattr(role, "name", role)
    return str(role or "").strip().lower()
'''


ENGINEER_ACTION = '''\n    @action(
        detail=False,
        methods=["get"],
        url_path="engineer-store",
        permission_classes=[IsAuthenticated],
    )
    def engineer_store(self, request):
        """Read-only, price-free In-Store list for the Engineer role."""
        if _request_active_role_name(request) != "engineer":
            raise PermissionDenied(
                "This In-Store view is available to Engineer users only."
            )

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(
            page if page is not None else queryset,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
'''


ENGINEER_PAGE_IMPORT = 'import EngineerInventoryPage from "./routes/EngineerInventoryPage";'

INVENTORY_ROUTE_COMPONENT = '''\nfunction InventoryRolePage() {
  const { user, activeRole } = useAuth();
  const role = String(
    activeRole ||
      user?.active_role ||
      user?.activeRole ||
      user?.role?.name ||
      user?.role ||
      "",
  ).trim().toLowerCase();

  return role === "engineer" ? <EngineerInventoryPage /> : <InventoryPage />;
}
'''


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.before_engineer_inventory_{stamp}.bak")


def patch_permissions(source: str) -> str:
    marker = "const WORK_MODULES = {"
    start = source.find(marker)
    if start < 0:
        raise ValueError("permissions.js: WORK_MODULES was not found.")
    block = source[start:]
    match = re.search(r"(?m)^([ \t]*engineer[ \t]*:[ \t]*\[)([\s\S]*?)(\])", block)
    if not match:
        raise ValueError("permissions.js: engineer work-module list was not found.")
    body = match.group(2)
    if re.search(r"['\"]inventory['\"]", body):
        return source
    item_match = re.search(r"(?m)^([ \t]*)['\"][^'\"]+['\"],?[ \t]*$", body)
    indent = item_match.group(1) if item_match else "    "
    trimmed = body.rstrip()
    closing_indent = body[len(trimmed):]
    new_body = trimmed + f"\n{indent}\"inventory\",\n" + closing_indent
    updated_block = block[:match.start(2)] + new_body + block[match.end(2):]
    return source[:start] + updated_block


def patch_sidebar(source: str) -> str:
    item_start = source.find('to: "/inventory"')
    if item_start < 0:
        raise ValueError("Sidebar.jsx: the Inventory navigation item was not found.")
    next_item = source.find("\n        {", item_start + 1)
    if next_item < 0:
        next_item = len(source)
    block = source[item_start:next_item]
    role_start = block.find("roles:")
    if role_start < 0:
        raise ValueError("Sidebar.jsx: Inventory role list was not found.")
    opening = block.find("[", role_start)
    closing = block.find("]", opening + 1)
    if opening < 0 or closing < 0:
        raise ValueError("Sidebar.jsx: Inventory role list is malformed.")
    role_text = block[opening + 1:closing]
    if re.search(r"['\"]engineer['\"]", role_text):
        return source
    item_match = re.search(r"(?m)^([ \t]*)['\"]inventory['\"],?[ \t]*$", role_text)
    if not item_match:
        raise ValueError("Sidebar.jsx: expected 'inventory' role entry was not found.")
    indent = item_match.group(1)
    role_text = role_text[:item_match.end()] + f"\n{indent}\"engineer\"," + role_text[item_match.end():]
    block = block[:opening + 1] + role_text + block[closing:]
    return source[:item_start] + block + source[next_item:]


def patch_app(source: str) -> str:
    inventory_import = 'import InventoryPage from "./routes/inventory";'
    if ENGINEER_PAGE_IMPORT not in source:
        if inventory_import not in source:
            raise ValueError("App.jsx: InventoryPage import was not found.")
        source = source.replace(
            inventory_import,
            inventory_import + "\n" + ENGINEER_PAGE_IMPORT,
            1,
        )

    role_match = re.search(r"(?s)(const INVENTORY_ROLES\s*=\s*\[)(.*?)(\])", source)
    if not role_match:
        raise ValueError("App.jsx: INVENTORY_ROLES was not found.")
    role_body = role_match.group(2)
    if not re.search(r"['\"]engineer['\"]", role_body):
        item_match = re.search(r"(?m)^([ \t]*)['\"]inventory['\"],?[ \t]*$", role_body)
        if not item_match:
            raise ValueError("App.jsx: expected 'inventory' entry was not found in INVENTORY_ROLES.")
        indent = item_match.group(1)
        role_body = role_body[:item_match.end()] + f"\n{indent}\"engineer\"," + role_body[item_match.end():]
        source = source[:role_match.start(2)] + role_body + source[role_match.end(2):]

    if "function InventoryRolePage()" not in source:
        app_anchor = "export default function App()"
        app_pos = source.find(app_anchor)
        if app_pos < 0:
            raise ValueError("App.jsx: App component declaration was not found.")
        source = source[:app_pos] + INVENTORY_ROUTE_COMPONENT + "\n" + source[app_pos:]

    route_pos = source.find('path="/inventory"')
    if route_pos < 0:
        raise ValueError("App.jsx: /inventory route was not found.")
    route_start = source.rfind("<Route", 0, route_pos)
    if route_start < 0 or source.startswith("<Routes", route_start):
        raise ValueError("App.jsx: /inventory route block could not be located.")

    # React Router commonly uses a self-closing <Route ... /> tag. Find its
    # closing marker at the same indentation so nested <InventoryPage /> tags
    # inside the element prop are not mistaken for the route's end.
    explicit_end = source.find("</Route>", route_pos)
    line_start = source.rfind("\n", 0, route_start) + 1
    route_indent = source[line_start:route_start]
    close_pattern = re.compile(
        r"(?m)^" + re.escape(route_indent) + r"/>[ \t]*(?:\n|$)"
    )
    self_closing = close_pattern.search(source, route_pos)
    if explicit_end >= 0 and (
        self_closing is None or explicit_end < self_closing.start()
    ):
        route_end = explicit_end + len("</Route>")
    elif self_closing:
        route_end = self_closing.end()
    else:
        raise ValueError("App.jsx: /inventory route block could not be located.")
    route = source[route_start:route_end]
    if "<InventoryRolePage />" not in route:
        if "<InventoryPage />" not in route:
            raise ValueError("App.jsx: InventoryPage route element was not found.")
        route = route.replace("<InventoryPage />", "<InventoryRolePage />", 1)
        source = source[:route_start] + route + source[route_end:]
    return source


def patch_views(source: str) -> str:
    if "EngineerInventorySerializer" not in source:
        source = source.replace(
            "from .serializers import (\n    InventorySerializer,",
            "from .serializers import (\n    InventorySerializer,\n    EngineerInventorySerializer,",
            1,
        )
        if "EngineerInventorySerializer" not in source:
            raise ValueError("inventory/views.py: serializer import block was not found.")

    if "from rest_framework.permissions import IsAuthenticated, SAFE_METHODS" not in source:
        if "from rest_framework.permissions import IsAuthenticated" not in source:
            raise ValueError("inventory/views.py: IsAuthenticated import was not found.")
        source = source.replace(
            "from rest_framework.permissions import IsAuthenticated",
            "from rest_framework.permissions import IsAuthenticated, SAFE_METHODS",
            1,
        )

    if "def _request_active_role_name(request):" not in source:
        anchor = "class InventoryViewSet(viewsets.ModelViewSet):"
        if anchor not in source:
            raise ValueError("inventory/views.py: InventoryViewSet was not found.")
        source = source.replace(anchor, ACTIVE_ROLE_HELPER + "\n\n" + anchor, 1)

    if "def engineer_store(self, request):" not in source:
        anchor = '    @action(detail=True, methods=["patch"], url_path="location", permission_classes=[IsAuthenticated])'
        if anchor not in source:
            raise ValueError("inventory/views.py: Inventory location action was not found.")
        source = source.replace(anchor, ENGINEER_ACTION + "\n" + anchor, 1)

    class_anchor = "    pagination_class = OptionalPageNumberPagination\n"
    methods = '''\n    def get_serializer_class(self):
        if (
            _request_active_role_name(self.request) == "engineer"
            and getattr(self, "action", "") in {"list", "retrieve", "engineer_store"}
        ):
            return EngineerInventorySerializer
        return super().get_serializer_class()

    def check_permissions(self, request):
        super().check_permissions(request)
        if _request_active_role_name(request) == "engineer":
            if request.method not in SAFE_METHODS:
                raise PermissionDenied("Engineer access to In-Store inventory is read-only.")
            if getattr(self, "action", "") not in {"list", "retrieve", "engineer_store"}:
                raise PermissionDenied("This Inventory action is not available to Engineer users.")
'''
    if "def get_serializer_class(self):" not in source[source.find("class InventoryViewSet"):source.find("class ProjectInventoryViewSet")]:
        pos = source.find(class_anchor, source.find("class InventoryViewSet"))
        if pos < 0:
            raise ValueError("inventory/views.py: InventoryViewSet pagination declaration was not found.")
        pos += len(class_anchor)
        source = source[:pos] + methods + source[pos:]

    list_anchor = "    def list(self, request, *args, **kwargs):\n        version = get_inventory_cache_version()"
    list_bypass = (
        "    def list(self, request, *args, **kwargs):\n"
        "        # Never share cached full Inventory responses with Engineer users.\n"
        "        if _request_active_role_name(request) == \"engineer\":\n"
        "            return super().list(request, *args, **kwargs)\n\n"
        "        version = get_inventory_cache_version()"
    )
    if "Never share cached full Inventory responses with Engineer users." not in source:
        if list_anchor not in source:
            raise ValueError("inventory/views.py: InventoryViewSet list method was not found.")
        source = source.replace(list_anchor, list_bypass, 1)

    queryset_anchor = (
        "    def get_queryset(self):\n"
        "        return apply_server_query_parameters(\n"
        "            super().get_queryset(),"
    )
    queryset_update = (
        "    def get_queryset(self):\n"
        "        queryset = super().get_queryset()\n"
        "        if _request_active_role_name(self.request) == \"engineer\":\n"
        "            queryset = queryset.filter(quantity__gt=0, issued=False)\n"
        "        return apply_server_query_parameters(\n"
        "            queryset,"
    )
    if "queryset = queryset.filter(quantity__gt=0, issued=False)" not in source:
        if queryset_anchor not in source:
            raise ValueError("inventory/views.py: InventoryViewSet get_queryset method was not found.")
        source = source.replace(queryset_anchor, queryset_update, 1)
    return source


def apply_changes() -> list[tuple[Path, str, str]]:
    backend_root = Path(__file__).resolve().parent
    if not (backend_root / "manage.py").is_file():
        raise ValueError("Place the script beside manage.py in the BE folder.")
    frontend_root = backend_root.parent / "FE" / "dream-to-life-app"
    page_source = Path(__file__).with_name("EngineerInventoryPage.jsx")
    if not page_source.is_file():
        raise ValueError("Keep EngineerInventoryPage.jsx beside this script.")

    sidebar_candidates = (
        frontend_root / "src" / "components" / "app" / "Sidebar.jsx",
        frontend_root / "src" / "components" / "Sidebar.jsx",
    )
    sidebar_path = next(
        (candidate for candidate in sidebar_candidates if candidate.is_file()),
        sidebar_candidates[0],
    )

    targets = {
        "permissions": frontend_root / "src" / "permissions.js",
        "sidebar": sidebar_path,
        "app": frontend_root / "src" / "App.jsx",
        "views": backend_root / "inventory" / "views.py",
        "serializers": backend_root / "inventory" / "serializers.py",
    }
    missing = [str(path) for path in targets.values() if not path.is_file()]
    if missing:
        raise ValueError("These expected project files were not found:\n" + "\n".join(missing))

    original: dict[str, tuple[str, str]] = {
        key: read_text(path) for key, path in targets.items()
    }
    updates = {
        "permissions": patch_permissions(original["permissions"][0]),
        "sidebar": patch_sidebar(original["sidebar"][0]),
        "app": patch_app(original["app"][0]),
        "views": patch_views(original["views"][0]),
        "serializers": original["serializers"][0],
    }
    if "class EngineerInventorySerializer" not in updates["serializers"]:
        updates["serializers"] = updates["serializers"].rstrip() + ENGINEER_SERIALIZER + "\n"

    page_target = frontend_root / "src" / "routes" / "EngineerInventoryPage.jsx"
    page_contents = page_source.read_text(encoding="utf-8")
    if page_target.exists() and page_target.read_text(encoding="utf-8") != page_contents:
        raise ValueError(
            f"{page_target} already exists with different content. Rename it before running this update."
        )

    changes: list[tuple[Path, str, str]] = []
    for key, path in targets.items():
        before, newline = original[key]
        after = updates[key]
        if before != after:
            changes.append((path, after.replace("\n", newline), before.replace("\n", newline)))
    if not page_target.exists():
        changes.append((page_target, page_contents, ""))
    return changes


def main() -> int:
    try:
        changes = apply_changes()
    except (OSError, ValueError) as error:
        print(f"No files were changed: {error}")
        return 1

    if not changes:
        print("Engineer In-Store Inventory update is already installed.")
        return 0

    backups: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for path, updated, original in changes:
            path.parent.mkdir(parents=True, exist_ok=True)
            if original:
                backup = backup_path(path)
                shutil.copy2(path, backup)
                backups.append((path, backup))
            else:
                created.append(path)
            path.write_text(updated, encoding="utf-8", newline="")
            print(f"Updated: {path}")
        for _, backup in backups:
            print(f"Backup:  {backup}")
        print("Restart Django and Vite, then sign in with the Engineer role.")
        return 0
    except OSError as error:
        print(f"Write failed: {error}")
        for path, backup in reversed(backups):
            try:
                shutil.copy2(backup, path)
                print(f"Restored: {path}")
            except OSError:
                print(f"Could not restore automatically: {path} (backup: {backup})")
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
                print(f"Removed newly created file: {path}")
            except OSError:
                print(f"Could not remove newly created file: {path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
