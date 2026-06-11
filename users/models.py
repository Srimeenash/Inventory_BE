from django.db import models


class User(models.Model):

    ROLE_CHOICES = [
        ("inventory", "Inventory"),
        ("procurement", "Procurement"),
        ("engineer", "Engineer"),
        ("finance", "Finance"),
        ("manager", "Manager"),
        ("admin", "Admin"),
    ]

    employee_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=128)

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES
    )

    designation = models.CharField(max_length=100, blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.employee_name} ({self.role})"