import uuid

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

    # ---------------------------------------------------------
    # FIRST-LOGIN EMAIL VERIFICATION
    # ---------------------------------------------------------
    # False:
    #   employee must verify the HR-provided official email once.
    #
    # True:
    #   future logins use email + IPMS password directly,
    #   without OTP.
    #
    # If Admin changes the employee email later, serializers.py
    # automatically resets this field to False.
    email_verified = models.BooleanField(
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


class LoginOTP(models.Model):
    """
    One-time verification challenge used to verify the employee's
    official HR-provided email during first login.

    The raw 6-digit code is never stored. Only a Django password hash
    of the code is saved in otp_hash.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="login_otps",
    )

    otp_hash = models.CharField(
        max_length=255
    )

    expires_at = models.DateTimeField()

    attempts = models.PositiveSmallIntegerField(
        default=0
    )

    max_attempts = models.PositiveSmallIntegerField(
        default=5
    )

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = [
            "-created_at"
        ]
        indexes = [
            models.Index(
                fields=[
                    "user",
                    "created_at",
                ]
            ),
        ]

    def __str__(self):
        return (
            f"LoginOTP({self.user.email}, "
            f"{self.created_at:%Y-%m-%d %H:%M:%S})"
        )