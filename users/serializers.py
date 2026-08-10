from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        min_length=4,
        style={"input_type": "password"},
    )

    remove_profile_image = serializers.BooleanField(
        write_only=True,
        required=False,
        default=False,
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "employee_name",
            "role",
            "designation",
            "profile_image",
            "remove_profile_image",
            "is_active",
            "is_staff",
            "is_superuser",
            "created_at",
            "updated_at",
            "password",
        ]

        read_only_fields = [
            "id",
            "is_staff",
            "is_superuser",
            "created_at",
            "updated_at",
        ]

    def validate_email(self, value):
        email = str(value or "").strip().lower()

        queryset = User.objects.filter(
            email__iexact=email
        )

        if self.instance:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )

        return email

    def validate_password(self, value):
        if len(value) < 4:
            raise serializers.ValidationError(
                "Password must contain at least 4 characters."
            )

        return value

    def validate_profile_image(self, image):
        if not image:
            return image

        maximum_size = 5 * 1024 * 1024

        if image.size > maximum_size:
            raise serializers.ValidationError(
                "Profile image must be 5 MB or smaller."
            )

        allowed_types = {
            "image/jpeg",
            "image/png",
            "image/webp",
        }

        content_type = getattr(
            image,
            "content_type",
            "",
        )

        if (
            content_type
            and content_type not in allowed_types
        ):
            raise serializers.ValidationError(
                "Use a JPG, PNG, or WEBP image."
            )

        return image

    def validate(self, attrs):
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

        return attrs

    def create(self, validated_data):
        password = validated_data.pop(
            "password"
        )

        validated_data.pop(
            "remove_profile_image",
            None,
        )

        role = str(
            validated_data.get("role") or ""
        ).lower()

        validated_data["is_staff"] = (
            role == "admin"
        )

        return User.objects.create_user(
            password=password,
            **validated_data,
        )

    def update(
        self,
        instance,
        validated_data,
    ):
        password = validated_data.pop(
            "password",
            None,
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

        if (
            remove_profile_image
            and instance.profile_image
        ):
            instance.profile_image.delete(
                save=False
            )
            instance.profile_image = None

        elif (
            new_profile_image
            and instance.profile_image
        ):
            instance.profile_image.delete(
                save=False
            )

        for attribute, value in (
            validated_data.items()
        ):
            setattr(
                instance,
                attribute,
                value,
            )

        if "role" in validated_data:
            role = str(
                validated_data.get("role")
                or ""
            ).lower()

            if not instance.is_superuser:
                instance.is_staff = (
                    role == "admin"
                )

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class EmailTokenSerializer(
    serializers.Serializer
):
    email = serializers.EmailField()
    password = serializers.CharField(
        trim_whitespace=False
    )

    def validate(self, data):
        email = str(
            data.get("email") or ""
        ).strip().lower()

        password = data.get("password")

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

        if not user.check_password(password):
            raise serializers.ValidationError(
                {
                    "detail":
                        "Incorrect password."
                }
            )

        if not user.is_active:
            raise serializers.ValidationError(
                {
                    "detail":
                        "User is inactive."
                }
            )

        data["user"] = user
        return data