import json
from pathlib import Path

from django.utils import timezone
from rest_framework import serializers

from .models import (
    LoginOTP,
    User,
)


# ============================================================
# USER SERIALIZER
# ============================================================

class UserSerializer(
    serializers.ModelSerializer
):

    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        min_length=4,
        style={
            "input_type": "password"
        },
    )

    remove_profile_image = (
        serializers.BooleanField(
            write_only=True,
            required=False,
            default=False,
        )
    )

    # ========================================================
    # MULTI-ROLE SUPPORT
    # ========================================================
    #
    # Example:
    #
    # Primary role:
    #     manager
    #
    # Additional roles:
    #     [
    #         "inventory",
    #         "procurement",
    #         "finance"
    #     ]
    #
    # `additional_roles` is stored in User.additional_roles.
    #
    # `roles` is a read-only combined list:
    #
    # [
    #     "manager",
    #     "inventory",
    #     "procurement",
    #     "finance"
    # ]
    #
    # ========================================================

    additional_roles = serializers.JSONField(
        required=False,
    )

    roles = serializers.SerializerMethodField(
        read_only=True,
    )


    class Meta:

        model = User

        fields = [
            "id",
            "email",
            "employee_name",

            # Primary role
            "role",

            # Multi-role fields
            "additional_roles",
            "roles",

            "designation",
            "profile_image",
            "remove_profile_image",
            "is_active",
            "is_staff",
            "is_superuser",
            "email_verified",
            "created_at",
            "updated_at",
            "password",
        ]

        read_only_fields = [
            "id",
            "is_staff",
            "is_superuser",
            "email_verified",
            "created_at",
            "updated_at",
            "roles",
        ]


    # ========================================================
    # GET ALL USER ROLES
    # ========================================================

    def get_roles(
        self,
        obj,
    ):
        """
        Return primary role + additional roles.

        Example:

        role:
            manager

        additional_roles:
            ["inventory", "procurement"]

        result:
            [
                "manager",
                "inventory",
                "procurement"
            ]
        """

        # Prefer helper from the corrected User model.
        if hasattr(
            obj,
            "get_all_roles",
        ):
            return obj.get_all_roles()

        # Backward-compatible fallback.
        roles = []

        primary_role = str(
            getattr(
                obj,
                "role",
                "",
            )
            or ""
        ).strip().lower()

        if primary_role:
            roles.append(
                primary_role
            )

        additional_roles = getattr(
            obj,
            "additional_roles",
            [],
        )

        if not isinstance(
            additional_roles,
            list,
        ):
            additional_roles = []

        for role in additional_roles:

            normalized_role = str(
                role or ""
            ).strip().lower()

            if (
                normalized_role
                and normalized_role
                not in roles
            ):
                roles.append(
                    normalized_role
                )

        return roles


    # ========================================================
    # EMAIL VALIDATION
    # ========================================================

    def validate_email(
        self,
        value,
    ):

        email = str(
            value or ""
        ).strip().lower()

        queryset = (
            User.objects.filter(
                email__iexact=email
            )
        )

        if self.instance:

            queryset = (
                queryset.exclude(
                    pk=self.instance.pk
                )
            )

        if queryset.exists():

            raise serializers.ValidationError(
                "A user with this email "
                "already exists."
            )

        return email


    # ========================================================
    # PASSWORD VALIDATION
    # ========================================================

    def validate_password(
        self,
        value,
    ):

        if len(value) < 4:

            raise serializers.ValidationError(
                "Password must contain "
                "at least 4 characters."
            )

        return value


    # ========================================================
    # PROFILE IMAGE VALIDATION
    # ========================================================

    def validate_profile_image(
        self,
        image,
    ):

        if not image:
            return image


        # ----------------------------------------------------
        # Maximum file size = 5 MB
        # ----------------------------------------------------

        maximum_size = (
            5 * 1024 * 1024
        )

        if image.size > maximum_size:

            raise serializers.ValidationError(
                "Profile image must be "
                "5 MB or smaller."
            )


        # ----------------------------------------------------
        # MIME type validation
        # ----------------------------------------------------

        allowed_types = {
            "image/jpeg",
            "image/png",
            "image/webp",
        }

        content_type = str(
            getattr(
                image,
                "content_type",
                "",
            )
            or ""
        ).strip().lower()

        if (
            content_type
            and content_type
            not in allowed_types
        ):

            raise serializers.ValidationError(
                "Only JPG, JPEG, PNG "
                "and WEBP images are allowed."
            )


        # ----------------------------------------------------
        # File extension validation
        # ----------------------------------------------------

        extension = Path(
            str(
                getattr(
                    image,
                    "name",
                    "",
                )
            )
        ).suffix.lower()

        allowed_extensions = {
            ".jpg",
            ".jpeg",
            ".jfif",
            ".png",
            ".webp",
        }

        if (
            not extension
            or extension
            not in allowed_extensions
        ):

            raise serializers.ValidationError(
                "Only JPG, JPEG, PNG "
                "and WEBP images are allowed."
            )

        return image


    # ========================================================
    # ADDITIONAL ROLE NORMALIZATION
    # ========================================================

    def normalize_additional_roles(
        self,
        value,
    ):
        """
        Convert incoming additional_roles into a clean list.

        Supports:

        JSON request:
            ["inventory", "procurement"]

        Multipart/FormData request:
            '["inventory", "procurement"]'
        """

        if value is None:
            return []

        # Multipart FormData may send JSON as text.
        if isinstance(
            value,
            str,
        ):

            raw_value = value.strip()

            if not raw_value:
                return []

            try:
                value = json.loads(
                    raw_value
                )

            except json.JSONDecodeError:

                raise serializers.ValidationError(
                    {
                        "additional_roles": [
                            "Additional roles must "
                            "be a valid list."
                        ]
                    }
                )

        if not isinstance(
            value,
            list,
        ):

            raise serializers.ValidationError(
                {
                    "additional_roles": [
                        "Additional roles must "
                        "be a list."
                    ]
                }
            )

        return value


    # ========================================================
    # GENERAL VALIDATION
    # ========================================================

    def validate(
        self,
        attrs,
    ):

        # ----------------------------------------------------
        # Password is mandatory when Admin creates a new user
        # ----------------------------------------------------

        if (
            self.instance is None
            and not attrs.get("password")
        ):

            raise serializers.ValidationError(
                {
                    "password": [
                        "Password is required."
                    ]
                }
            )


        # ----------------------------------------------------
        # Resolve primary role
        # ----------------------------------------------------

        if "role" in attrs:

            primary_role = str(
                attrs.get(
                    "role",
                    "",
                )
                or ""
            ).strip().lower()

        elif self.instance:

            primary_role = str(
                self.instance.role
                or ""
            ).strip().lower()

        else:

            primary_role = ""


        # ----------------------------------------------------
        # Validate primary role
        # ----------------------------------------------------

        allowed_roles = {
            str(code).strip().lower()
            for code, _label
            in User.ROLE_CHOICES
        }

        if (
            primary_role
            and primary_role
            not in allowed_roles
        ):

            raise serializers.ValidationError(
                {
                    "role": [
                        "Invalid primary role."
                    ]
                }
            )


        # ----------------------------------------------------
        # Resolve additional roles
        # ----------------------------------------------------

        if "additional_roles" in attrs:

            additional_roles = (
                self.normalize_additional_roles(
                    attrs.get(
                        "additional_roles"
                    )
                )
            )

        elif self.instance:

            additional_roles = (
                getattr(
                    self.instance,
                    "additional_roles",
                    [],
                )
                or []
            )

        else:

            additional_roles = []


        # ----------------------------------------------------
        # Normalize / validate every additional role
        # ----------------------------------------------------

        normalized_roles = []

        for role in additional_roles:

            normalized_role = str(
                role or ""
            ).strip().lower()

            if not normalized_role:
                continue


            # -----------------------------------------------
            # Role must exist
            # -----------------------------------------------

            if (
                normalized_role
                not in allowed_roles
            ):

                raise serializers.ValidationError(
                    {
                        "additional_roles": [
                            (
                                "Invalid additional "
                                f"role: {normalized_role}"
                            )
                        ]
                    }
                )


            # -----------------------------------------------
            # Do not duplicate primary role
            # -----------------------------------------------

            if (
                normalized_role
                == primary_role
            ):
                continue


            # -----------------------------------------------
            # Prevent duplicates
            # -----------------------------------------------

            if (
                normalized_role
                not in normalized_roles
            ):

                normalized_roles.append(
                    normalized_role
                )


        # ====================================================
        # ENGINEER RESTRICTION
        # ====================================================
        #
        # Requirement:
        #
        # Engineer is SINGLE ROLE only.
        #
        # NOT ALLOWED:
        #
        # Engineer + Inventory
        # Engineer + Procurement
        # Engineer + Manager
        # Engineer + Finance
        # Engineer + Admin
        #
        # ====================================================

        if (
            primary_role == "engineer"
            and normalized_roles
        ):

            raise serializers.ValidationError(
                {
                    "additional_roles": [
                        (
                            "Engineer users cannot "
                            "have additional role access."
                        )
                    ]
                }
            )


        # ----------------------------------------------------
        # Engineer cannot be an additional role either
        # ----------------------------------------------------

        if (
            "engineer"
            in normalized_roles
        ):

            raise serializers.ValidationError(
                {
                    "additional_roles": [
                        (
                            "Engineer cannot be "
                            "assigned as an "
                            "additional role."
                        )
                    ]
                }
            )


        # ----------------------------------------------------
        # Store clean role list
        # ----------------------------------------------------

        attrs[
            "additional_roles"
        ] = normalized_roles


        return attrs


    # ========================================================
    # CREATE USER
    # ========================================================

    def create(
        self,
        validated_data,
    ):

        password = (
            validated_data.pop(
                "password"
            )
        )

        validated_data.pop(
            "remove_profile_image",
            None,
        )


        # ----------------------------------------------------
        # Ensure additional_roles is always a list
        # ----------------------------------------------------

        additional_roles = (
            validated_data.get(
                "additional_roles",
                [],
            )
            or []
        )

        validated_data[
            "additional_roles"
        ] = additional_roles


        # ----------------------------------------------------
        # Existing primary-role / staff handling
        # ----------------------------------------------------

        role = str(
            validated_data.get(
                "role"
            )
            or ""
        ).lower()

        validated_data[
            "is_staff"
        ] = (
            role == "admin"
        )


        # ----------------------------------------------------
        # email_verified uses model default=False.
        #
        # Therefore every employee created by Admin
        # must verify the official email once.
        # ----------------------------------------------------

        return User.objects.create_user(
            password=password,
            **validated_data,
        )


    # ========================================================
    # UPDATE USER
    # ========================================================

    def update(
        self,
        instance,
        validated_data,
    ):

        password = (
            validated_data.pop(
                "password",
                None,
            )
        )

        remove_profile_image = (
            validated_data.pop(
                "remove_profile_image",
                False,
            )
        )

        new_profile_image = (
            validated_data.get(
                "profile_image"
            )
        )


        # ----------------------------------------------------
        # Remember old image information
        #
        # IMPORTANT:
        # Do NOT delete the old image before the new user
        # record/image has saved successfully.
        # ----------------------------------------------------

        old_image_name = None
        old_image_storage = None

        if instance.profile_image:

            old_image_name = (
                instance.profile_image.name
            )

            old_image_storage = (
                instance.profile_image.storage
            )


        # ----------------------------------------------------
        # Detect email changes
        # ----------------------------------------------------

        old_email = str(
            instance.email or ""
        ).strip().lower()

        incoming_email = (
            validated_data.get(
                "email",
                instance.email,
            )
        )

        new_email = str(
            incoming_email or ""
        ).strip().lower()

        email_changed = (
            old_email != new_email
        )


        # ----------------------------------------------------
        # Explicit profile image removal
        # ----------------------------------------------------

        if remove_profile_image:

            # If both remove=true and an uploaded file were
            # somehow supplied, removal takes priority.

            validated_data.pop(
                "profile_image",
                None,
            )

            new_profile_image = None

            instance.profile_image = None


        # ----------------------------------------------------
        # Update normal fields
        # ----------------------------------------------------

        for (
            attribute,
            value,
        ) in validated_data.items():

            setattr(
                instance,
                attribute,
                value,
            )


        # ----------------------------------------------------
        # Role / staff handling
        # ----------------------------------------------------

        if "role" in validated_data:

            role = str(
                validated_data.get(
                    "role"
                )
                or ""
            ).lower()

            if not instance.is_superuser:

                instance.is_staff = (
                    role == "admin"
                )


        # ----------------------------------------------------
        # Safety:
        #
        # If Engineer somehow reaches this stage,
        # guarantee additional roles are empty.
        # ----------------------------------------------------

        current_role = str(
            instance.role or ""
        ).strip().lower()

        if current_role == "engineer":

            instance.additional_roles = []


        # ----------------------------------------------------
        # Password update
        # ----------------------------------------------------

        if password:

            instance.set_password(
                password
            )


        # ----------------------------------------------------
        # Email verification
        # ----------------------------------------------------

        if email_changed:

            instance.email_verified = False


        # ----------------------------------------------------
        # SAVE USER
        #
        # New image is saved here first.
        # If saving fails, old file still exists.
        # ----------------------------------------------------

        instance.save()


        # ----------------------------------------------------
        # Delete previous physical image only AFTER the new
        # record has saved successfully.
        # ----------------------------------------------------

        replacing_image = bool(
            new_profile_image
        )

        deleting_image = bool(
            remove_profile_image
        )

        if (
            old_image_name
            and old_image_storage
            and (
                replacing_image
                or deleting_image
            )
        ):

            if instance.profile_image:

                current_image_name = (
                    instance
                    .profile_image
                    .name
                )

            else:

                current_image_name = None


            if (
                old_image_name
                != current_image_name
            ):

                try:

                    old_image_storage.delete(
                        old_image_name
                    )

                except Exception:

                    # File cleanup must not cause the
                    # whole profile update to fail.
                    pass


        # ----------------------------------------------------
        # Invalidate previous OTPs if email changed
        # ----------------------------------------------------

        if email_changed:

            LoginOTP.objects.filter(
                user=instance,
                used_at__isnull=True,
            ).update(
                used_at=timezone.now()
            )


        return instance


# ============================================================
# LOGIN SERIALIZER
# ============================================================

class EmailTokenSerializer(
    serializers.Serializer
):

    email = serializers.EmailField()

    password = serializers.CharField(
        trim_whitespace=False
    )


    def validate(
        self,
        data,
    ):

        email = str(
            data.get("email")
            or ""
        ).strip().lower()

        password = data.get(
            "password"
        )


        try:

            user = User.objects.get(
                email__iexact=email
            )

        except User.DoesNotExist:

            raise serializers.ValidationError(
                {
                    "detail": (
                        "No account found with "
                        "this email address."
                    )
                }
            )


        if not user.check_password(
            password
        ):

            raise serializers.ValidationError(
                {
                    "detail": (
                        "Incorrect password."
                    )
                }
            )


        if not user.is_active:

            raise serializers.ValidationError(
                {
                    "detail": (
                        "User is inactive."
                    )
                }
            )


        data["user"] = user

        return data


# ============================================================
# LOGIN OTP VERIFY SERIALIZER
# ============================================================

class LoginOTPVerifySerializer(
    serializers.Serializer
):

    verification_id = (
        serializers.UUIDField()
    )

    code = serializers.RegexField(
        regex=r"^\d{6}$",
        error_messages={
            "invalid": (
                "Enter the 6-digit "
                "verification code."
            )
        },
    )


# ============================================================
# LOGIN OTP RESEND SERIALIZER
# ============================================================

class LoginOTPResendSerializer(
    serializers.Serializer
):

    verification_id = (
        serializers.UUIDField()
    )