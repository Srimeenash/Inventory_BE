from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models


class UserManager(BaseUserManager):
    def create_user(
        self,
        email,
        password=None,
        **extra_fields,
    ):
        if not email:
            raise ValueError("Email is required.")

        email = self.normalize_email(email).lower()

        user = self.model(
            email=email,
            **extra_fields,
        )

        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email,
        password=None,
        **extra_fields,
    ):
        extra_fields.setdefault(
            "is_staff",
            True,
        )
        extra_fields.setdefault(
            "is_superuser",
            True,
        )
        extra_fields.setdefault(
            "is_active",
            True,
        )
        extra_fields.setdefault(
            "role",
            "admin",
        )

        if (
            extra_fields.get("is_staff")
            is not True
        ):
            raise ValueError(
                "Superuser must have is_staff=True."
            )

        if (
            extra_fields.get("is_superuser")
            is not True
        ):
            raise ValueError(
                "Superuser must have "
                "is_superuser=True."
            )

        return self.create_user(
            email,
            password,
            **extra_fields,
        )


class User(
    AbstractBaseUser,
    PermissionsMixin,
):
    ROLE_CHOICES = [
        ("inventory", "Inventory"),
        ("procurement", "Procurement"),
        ("engineer", "Engineer"),
        ("finance", "Finance"),
        ("manager", "Manager"),
        ("admin", "Admin"),
    ]

    email = models.EmailField(
        unique=True
    )

    employee_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        blank=True,
        null=True,
    )

    designation = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    profile_image = models.ImageField(
        upload_to="profile_images/",
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(
        default=True
    )

    is_staff = models.BooleanField(
        default=False
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email