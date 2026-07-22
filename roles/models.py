from django.db import models


class Role(models.Model):
    ROLE_CHOICES = [
    ('ADMIN', 'Admin'),
    ('SUB_ADMIN', 'Sub Admin'),

    ('MANAGER', 'Manager'),

    ('PROCUREMENT_MANAGER', 'Procurement Manager'),
    ('PROCUREMENT_EXECUTIVE', 'Procurement Executive'),

    ('INVENTORY_MANAGER', 'Inventory Manager'),
    ('INVENTORY_EXECUTIVE', 'Inventory Executive'),

    ('ENGINEERING_MANAGER', 'Engineering Manager'),
    ('ENGINEER', 'Engineer'),

    ('FINANCE_MANAGER', 'Finance Manager'),
    ('FINANCE_EXECUTIVE', 'Finance Executive'),
]

    module = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    name = models.CharField(
        max_length=50,
        choices=ROLE_CHOICES,
        unique=True
    )

    description = models.TextField(
        blank=True,
        null=True
    )

    is_active = models.BooleanField(
        default=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return self.name