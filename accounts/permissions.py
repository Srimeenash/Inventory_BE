from rest_framework import permissions


class RolePermission(permissions.BasePermission):
    allowed_roles = []

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        role = getattr(request.user, 'role', None)
        return bool(role and role.name in self.allowed_roles)


class IsAdmin(RolePermission):
    allowed_roles = ['ADMIN']


class IsProcurementExecutive(RolePermission):
    allowed_roles = ['ADMIN', 'PROCUREMENT_MANAGER', 'PROCUREMENT_EXECUTIVE']


class IsInventoryExecutive(RolePermission):
    allowed_roles = ['ADMIN', 'INVENTORY_MANAGER', 'INVENTORY_EXECUTIVE']


class IsEngineeringUser(RolePermission):
    allowed_roles = ['ADMIN', 'ENGINEERING_MANAGER', 'ENGINEER']


class IsFinanceExecutive(RolePermission):
    allowed_roles = ['ADMIN', 'FINANCE_MANAGER', 'FINANCE_EXECUTIVE']


class IsManager(RolePermission):
    allowed_roles = [
        'ADMIN',
        'PROCUREMENT_MANAGER',
        'INVENTORY_MANAGER',
        'ENGINEERING_MANAGER',
        'FINANCE_MANAGER',
    ]


class IsApprovalAuthority(RolePermission):
    allowed_roles = [
        'ADMIN',
        'PROCUREMENT_MANAGER',
        'INVENTORY_MANAGER',
        'ENGINEERING_MANAGER',
        'FINANCE_MANAGER',
    ]
