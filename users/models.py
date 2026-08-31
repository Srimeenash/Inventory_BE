import uuid
from pathlib import Path

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models


# ============================================================
# PROFILE IMAGE UPLOAD
# ============================================================

def profile_image_upload_path(instance, filename):
    """
    Store profile pictures with a short unique filename.

    Example:
        profile_images/
        09a94914998241f294dd221fdc29a208.jpg

    This prevents errors caused by very long original
    filenames and duplicate filenames.
    """

    extension = Path(
        str(filename or "")
    ).suffix.lower()

    allowed_extensions = {
        ".jpg",
        ".jpeg",
        ".jfif",
        ".png",
        ".webp",
    }

    # The serializer validates the real extension before
    # reaching this function. This is only a safe fallback.
    if extension not in allowed_extensions:
        extension = ".jpg"

    return (
        f"profile_images/"
        f"{uuid.uuid4().hex}"
        f"{extension}"
    )


# ============================================================
# USER MANAGER
# ============================================================

class UserManager(BaseUserManager):

    def create_user(
        self,
        email,
        password=None,
        **extra_fields,
    ):
        if not email:
            raise ValueError(
                "Email is required."
            )

        email = (
            self.normalize_email(email)
            .strip()
            .lower()
        )

        user = self.model(
            email=email,
            **extra_fields,
        )

        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(
            using=self._db
        )

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

        # Superuser does not require additional roles.
        # Superuser already has complete system access.
        extra_fields.setdefault(
            "additional_roles",
            [],
        )

        if (
            extra_fields.get("is_staff")
            is not True
        ):
            raise ValueError(
                "Superuser must have "
                "is_staff=True."
            )

        if (
            extra_fields.get(
                "is_superuser"
            )
            is not True
        ):
            raise ValueError(
                "Superuser must have "
                "is_superuser=True."
            )

        return self.create_user(
            email=email,
            password=password,
            **extra_fields,
        )


# ============================================================
# USER MODEL
# ============================================================

class User(
    AbstractBaseUser,
    PermissionsMixin,
):

    # --------------------------------------------------------
    # PRIMARY ROLE OPTIONS
    # --------------------------------------------------------

    ROLE_CHOICES = [
        (
            "inventory",
            "Inventory",
        ),
        (
            "procurement",
            "Procurement",
        ),
        (
            "engineer",
            "Engineer",
        ),
        (
            "finance",
            "Finance",
        ),
        (
            "manager",
            "Manager",
        ),
        (
            "management",
            "Management",
        ),
        (
            "admin",
            "Admin",
        ),
    ]


    # --------------------------------------------------------
    # BASIC USER DETAILS
    # --------------------------------------------------------

    email = models.EmailField(
        unique=True,
    )

    employee_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
    )


    # ========================================================
    # PRIMARY ROLE
    # ========================================================
    #
    # IMPORTANT:
    #
    # Keep this existing field.
    #
    # Existing IPMS pages already depend on:
    #
    #     user.role
    #
    # Example:
    #
    #     manager
    #     inventory
    #     procurement
    #
    # Therefore this field remains the employee's
    # PRIMARY ROLE.
    # ========================================================

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        blank=True,
        null=True,
    )


    # ========================================================
    # ADDITIONAL ROLE ACCESS
    # ========================================================
    #
    # NEW FIELD
    #
    # Allows one employee account to access multiple roles.
    #
    # Example:
    #
    # Primary role:
    #
    #     manager
    #
    # Additional roles:
    #
    #     [
    #         "inventory",
    #         "procurement",
    #         "finance"
    #     ]
    #
    #
    # ENGINEER RULE:
    #
    # Engineer users must remain single-role users.
    #
    # This business rule will also be enforced by
    # serializers.py so users cannot bypass it through API.
    #
    # We are using JSONField instead of replacing the existing
    # role field, which keeps the current IPMS code compatible.
    # ========================================================

    additional_roles = models.JSONField(
        default=list,
        blank=True,
    )


    designation = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )


    # --------------------------------------------------------
    # PROFILE IMAGE
    # --------------------------------------------------------

    profile_image = models.ImageField(
        upload_to=profile_image_upload_path,
        max_length=255,
        blank=True,
        null=True,
    )


    # --------------------------------------------------------
    # ACCOUNT STATUS
    # --------------------------------------------------------

    is_active = models.BooleanField(
        default=True,
    )

    is_staff = models.BooleanField(
        default=False,
    )


    # --------------------------------------------------------
    # FIRST-LOGIN EMAIL VERIFICATION
    # --------------------------------------------------------
    #
    # False:
    #   Employee must verify the HR-provided official email.
    #
    # True:
    #   Future logins use email + password without OTP.
    #
    # If Admin changes the email later,
    # serializers.py resets this to False.
    # --------------------------------------------------------

    email_verified = models.BooleanField(
        default=False,
    )


    # --------------------------------------------------------
    # TIMESTAMPS
    # --------------------------------------------------------

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )


    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = []

    objects = UserManager()


    # ========================================================
    # ALL ASSIGNED ROLES
    # ========================================================
    #
    # Returns:
    #
    # Primary:
    #     manager
    #
    # Additional:
    #     inventory
    #     procurement
    #
    # Result:
    #
    # [
    #     "manager",
    #     "inventory",
    #     "procurement"
    # ]
    #
    # This function will be useful in serializers,
    # JWT generation and role switching.
    # ========================================================

    def get_all_roles(self):
        roles = []

        primary_role = str(
            self.role or ""
        ).strip().lower()

        if primary_role:
            roles.append(
                primary_role
            )

        additional_roles = (
            self.additional_roles
            if isinstance(
                self.additional_roles,
                list,
            )
            else []
        )

        for role in additional_roles:

            normalized_role = str(
                role or ""
            ).strip().lower()

            if not normalized_role:
                continue

            if normalized_role not in roles:
                roles.append(
                    normalized_role
                )

        return roles


    # ========================================================
    # CHECK WHETHER USER HAS ROLE
    # ========================================================

    def has_ipms_role(
        self,
        role,
    ):
        normalized_role = str(
            role or ""
        ).strip().lower()

        return (
            normalized_role
            in self.get_all_roles()
        )


    # ========================================================
    # CAN USER HAVE MULTIPLE ROLES?
    # ========================================================

    @property
    def can_have_multiple_roles(self):
        """
        Engineer accounts are intentionally restricted
        to a single Engineer role.

        Other IPMS account types can be assigned
        additional roles by Admin.
        """

        primary_role = str(
            self.role or ""
        ).strip().lower()

        return (
            primary_role != "engineer"
        )


    def __str__(self):
        return self.email


# ============================================================
# LOGIN OTP MODEL
# ============================================================

class LoginOTP(models.Model):
    """
    One-time verification challenge used to verify the
    employee's official HR-provided email during first login.

    The actual 6-digit OTP is never stored.

    Only a Django password hash of the OTP is saved.
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
        max_length=255,
    )

    expires_at = models.DateTimeField()

    attempts = (
        models.PositiveSmallIntegerField(
            default=0,
        )
    )

    max_attempts = (
        models.PositiveSmallIntegerField(
            default=5,
        )
    )

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )


    class Meta:

        ordering = [
            "-created_at",
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
            f"LoginOTP("
            f"{self.user.email}, "
            f"{self.created_at:%Y-%m-%d %H:%M:%S}"
            f")"
        )