import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import (
    check_password,
    make_password,
)
from django.db import transaction
from django.utils import timezone

from rest_framework import (
    permissions,
    status,
)
from rest_framework.parsers import (
    FormParser,
    JSONParser,
    MultiPartParser,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import (
    JWTAuthentication,
)
from rest_framework_simplejwt.tokens import (
    RefreshToken,
)

from notifications.email_service import (
    send_login_otp_email,
)

from .models import (
    LoginOTP,
    User,
)
from .serializers import (
    EmailTokenSerializer,
    LoginOTPResendSerializer,
    LoginOTPVerifySerializer,
    UserSerializer,
)


def get_otp_expiry_minutes():
    return int(
        getattr(
            settings,
            "LOGIN_OTP_EXPIRY_MINUTES",
            5,
        )
    )


def get_otp_max_attempts():
    return int(
        getattr(
            settings,
            "LOGIN_OTP_MAX_ATTEMPTS",
            5,
        )
    )


def get_otp_resend_cooldown_seconds():
    return int(
        getattr(
            settings,
            "LOGIN_OTP_RESEND_COOLDOWN_SECONDS",
            60,
        )
    )


def generate_login_code():
    return str(
        100000
        + secrets.randbelow(900000)
    )


def create_login_otp(user):
    now = timezone.now()

    # Any older, still-open code becomes unusable once a new code is created.
    LoginOTP.objects.filter(
        user=user,
        used_at__isnull=True,
    ).update(
        used_at=now
    )

    code = generate_login_code()

    otp = LoginOTP.objects.create(
        user=user,
        otp_hash=make_password(code),
        expires_at=(
            now
            + timedelta(
                minutes=get_otp_expiry_minutes()
            )
        ),
        max_attempts=get_otp_max_attempts(),
    )

    return otp, code


def seconds_until_resend(otp):
    cooldown = (
        get_otp_resend_cooldown_seconds()
    )

    elapsed = (
        timezone.now()
        - otp.created_at
    ).total_seconds()

    return max(
        0,
        int(cooldown - elapsed),
    )


def seconds_until_expiry(otp):
    remaining = (
        otp.expires_at
        - timezone.now()
    ).total_seconds()

    return max(
        0,
        int(remaining),
    )


def serialize_user(
    user,
    request,
):
    return UserSerializer(
        user,
        context={
            "request": request
        },
    ).data


def token_response_for_user(
    user,
    request,
):
    if not user.is_active:
        return None

    refresh = RefreshToken.for_user(
        user
    )

    return {
        "access": str(
            refresh.access_token
        ),
        "refresh": str(refresh),
        "user": serialize_user(
            user,
            request,
        ),
    }


class IsAdminRole(
    permissions.BasePermission
):
    message = (
        "Only an active IPMS admin can "
        "manage users."
    )

    def has_permission(
        self,
        request,
        view,
    ):
        user = request.user

        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and (
                user.is_superuser
                or str(
                    user.role or ""
                ).lower()
                == "admin"
            )
        )


class LoginView(APIView):
    authentication_classes = [
        JWTAuthentication
    ]

    parser_classes = [
        JSONParser,
    ]

    def get_permissions(self):
        if self.request.method == "POST":
            return [
                permissions.AllowAny()
            ]

        return [
            IsAdminRole()
        ]

    def get(self, request):
        """
        Backward-compatible admin user list.

        The new frontend uses /auth/users/,
        but /auth/login/ GET is retained so
        existing pages do not break.
        """
        users = User.objects.all().order_by(
            "-created_at"
        )

        serializer = UserSerializer(
            users,
            many=True,
            context={
                "request": request
            },
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        """
        Login rules:

        1. Validate official HR-provided email + IPMS password.
        2. If user.email_verified is already True:
              return JWT immediately (NO OTP).
        3. If user.email_verified is False:
              send first-login OTP and wait for verification.
        """
        serializer = EmailTokenSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = serializer.validated_data[
            "user"
        ]

        # -----------------------------------------------------
        # ALREADY VERIFIED
        # -----------------------------------------------------
        # OTP is NOT required on second and later logins.
        if user.email_verified:
            payload = token_response_for_user(
                user,
                request,
            )

            payload["verification_required"] = False

            return Response(
                payload,
                status=status.HTTP_200_OK,
            )

        # -----------------------------------------------------
        # FIRST LOGIN / EMAIL CHANGED
        # -----------------------------------------------------
        # The employee must prove access to the official
        # HR-provided Zoho email once.
        now = timezone.now()

        current_otp = (
            LoginOTP.objects.filter(
                user=user,
                used_at__isnull=True,
                expires_at__gt=now,
            )
            .order_by("-created_at")
            .first()
        )

        # If a valid code was just sent, do not create/send
        # another code until the resend cooldown is over.
        if (
            current_otp
            and seconds_until_resend(
                current_otp
            ) > 0
        ):
            return Response(
                {
                    "verification_required": True,
                    "verification_id": str(
                        current_otp.id
                    ),
                    "email": user.email,
                    "expires_in": (
                        seconds_until_expiry(
                            current_otp
                        )
                    ),
                    "resend_after": (
                        seconds_until_resend(
                            current_otp
                        )
                    ),
                    "detail": (
                        "A verification code was "
                        "already sent to your "
                        "official email address."
                    ),
                },
                status=status.HTTP_200_OK,
            )

        otp, code = create_login_otp(
            user
        )

        sent = send_login_otp_email(
            recipient_email=user.email,
            recipient_name=(
                user.employee_name
                or user.email
            ),
            code=code,
            expiry_minutes=(
                get_otp_expiry_minutes()
            ),
        )

        if not sent:
            otp.delete()

            return Response(
                {
                    "detail": (
                        "Unable to send the login "
                        "verification email. "
                        "Please contact IPMS Admin."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "verification_required": True,
                "verification_id": str(
                    otp.id
                ),
                "email": user.email,
                "expires_in": (
                    seconds_until_expiry(
                        otp
                    )
                ),
                "resend_after": (
                    get_otp_resend_cooldown_seconds()
                ),
                "detail": (
                    "First-login verification code "
                    "sent to your official email."
                ),
            },
            status=status.HTTP_200_OK,
        )


class LoginVerifyView(APIView):
    authentication_classes = []
    permission_classes = [
        permissions.AllowAny
    ]
    parser_classes = [
        JSONParser
    ]

    def post(self, request):
        """
        Step 2 of login:
        verify the emailed code and only then issue JWT tokens.
        """
        serializer = LoginOTPVerifySerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        verification_id = (
            serializer.validated_data[
                "verification_id"
            ]
        )
        code = serializer.validated_data[
            "code"
        ]

        with transaction.atomic():
            try:
                otp = (
                    LoginOTP.objects
                    .select_for_update()
                    .select_related("user")
                    .get(
                        pk=verification_id
                    )
                )
            except LoginOTP.DoesNotExist:
                return Response(
                    {
                        "detail": (
                            "Verification request "
                            "not found. Please sign "
                            "in again."
                        )
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

            now = timezone.now()

            if otp.used_at is not None:
                return Response(
                    {
                        "detail": (
                            "This verification code "
                            "has already been used. "
                            "Please sign in again."
                        )
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

            if now >= otp.expires_at:
                otp.used_at = now
                otp.save(
                    update_fields=[
                        "used_at"
                    ]
                )

                return Response(
                    {
                        "detail": (
                            "Verification code "
                            "expired. Please sign "
                            "in again."
                        )
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

            if (
                otp.attempts
                >= otp.max_attempts
            ):
                otp.used_at = now
                otp.save(
                    update_fields=[
                        "used_at"
                    ]
                )

                return Response(
                    {
                        "detail": (
                            "Too many incorrect "
                            "attempts. Please sign "
                            "in again."
                        )
                    },
                    status=(
                        status.HTTP_429_TOO_MANY_REQUESTS
                    ),
                )

            if not check_password(
                code,
                otp.otp_hash,
            ):
                otp.attempts += 1

                update_fields = [
                    "attempts"
                ]

                remaining_attempts = max(
                    0,
                    otp.max_attempts
                    - otp.attempts,
                )

                if remaining_attempts == 0:
                    otp.used_at = now
                    update_fields.append(
                        "used_at"
                    )

                otp.save(
                    update_fields=update_fields
                )

                if remaining_attempts == 0:
                    return Response(
                        {
                            "detail": (
                                "Too many incorrect "
                                "attempts. Please "
                                "sign in again."
                            )
                        },
                        status=(
                            status.HTTP_429_TOO_MANY_REQUESTS
                        ),
                    )

                return Response(
                    {
                        "detail": (
                            "Incorrect verification "
                            f"code. {remaining_attempts} "
                            "attempt(s) remaining."
                        )
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

            user = otp.user

            if not user.is_active:
                otp.used_at = now
                otp.save(
                    update_fields=[
                        "used_at"
                    ]
                )

                return Response(
                    {
                        "detail":
                            "User is inactive."
                    },
                    status=(
                        status.HTTP_403_FORBIDDEN
                    ),
                )

            otp.used_at = now
            otp.save(
                update_fields=[
                    "used_at"
                ]
            )

            # Successful first-login verification permanently
            # verifies this official email address.
            user.email_verified = True
            user.save(
                update_fields=[
                    "email_verified"
                ]
            )

            payload = token_response_for_user(
                user,
                request,
            )

            payload["verification_required"] = False

        return Response(
            payload,
            status=status.HTTP_200_OK,
        )


class LoginResendView(APIView):
    authentication_classes = []
    permission_classes = [
        permissions.AllowAny
    ]
    parser_classes = [
        JSONParser
    ]

    def post(self, request):
        serializer = LoginOTPResendSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        verification_id = (
            serializer.validated_data[
                "verification_id"
            ]
        )

        try:
            current_otp = (
                LoginOTP.objects
                .select_related("user")
                .get(
                    pk=verification_id
                )
            )
        except LoginOTP.DoesNotExist:
            return Response(
                {
                    "detail": (
                        "Verification request "
                        "not found. Please sign "
                        "in again."
                    )
                },
                status=(
                    status.HTTP_400_BAD_REQUEST
                ),
            )

        user = current_otp.user

        if user.email_verified:
            return Response(
                {
                    "detail": (
                        "This official email is already "
                        "verified. Please sign in normally."
                    )
                },
                status=(
                    status.HTTP_400_BAD_REQUEST
                ),
            )

        if not user.is_active:
            return Response(
                {
                    "detail":
                        "User is inactive."
                },
                status=(
                    status.HTTP_403_FORBIDDEN
                ),
            )

        wait_seconds = (
            seconds_until_resend(
                current_otp
            )
        )

        if wait_seconds > 0:
            return Response(
                {
                    "detail": (
                        "Please wait before "
                        "requesting another code."
                    ),
                    "retry_after": wait_seconds,
                },
                status=(
                    status.HTTP_429_TOO_MANY_REQUESTS
                ),
            )

        otp, code = create_login_otp(
            user
        )

        sent = send_login_otp_email(
            recipient_email=user.email,
            recipient_name=(
                user.employee_name
                or user.email
            ),
            code=code,
            expiry_minutes=(
                get_otp_expiry_minutes()
            ),
        )

        if not sent:
            otp.delete()

            return Response(
                {
                    "detail": (
                        "Unable to resend the "
                        "verification email. "
                        "Please contact IPMS Admin."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "verification_required": True,
                "verification_id": str(
                    otp.id
                ),
                "email": user.email,
                "expires_in": (
                    seconds_until_expiry(
                        otp
                    )
                ),
                "resend_after": (
                    get_otp_resend_cooldown_seconds()
                ),
                "detail": (
                    "A new verification code "
                    "was sent."
                ),
            },
            status=status.HTTP_200_OK,
        )


class UserListCreateView(APIView):
    authentication_classes = [
        JWTAuthentication
    ]

    permission_classes = [
        IsAdminRole
    ]

    parser_classes = [
        MultiPartParser,
        FormParser,
        JSONParser,
    ]

    def get(self, request):
        users = User.objects.all().order_by(
            "-created_at"
        )

        serializer = UserSerializer(
            users,
            many=True,
            context={
                "request": request
            },
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        serializer = UserSerializer(
            data=request.data,
            context={
                "request": request
            },
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = serializer.save()

        return Response(
            serialize_user(
                user,
                request,
            ),
            status=status.HTTP_201_CREATED,
        )


class RegisterView(
    UserListCreateView
):
    """
    Backward-compatible route.

    User registration is admin-controlled.
    """


class UserDetailView(APIView):
    authentication_classes = [
        JWTAuthentication
    ]

    permission_classes = [
        IsAdminRole
    ]

    parser_classes = [
        MultiPartParser,
        FormParser,
        JSONParser,
    ]

    @staticmethod
    def get_object(pk):
        try:
            return User.objects.get(
                pk=pk
            )
        except User.DoesNotExist:
            return None

    def get(self, request, pk):
        user = self.get_object(pk)

        if user is None:
            return Response(
                {
                    "detail":
                        "User not found."
                },
                status=(
                    status.HTTP_404_NOT_FOUND
                ),
            )

        return Response(
            serialize_user(
                user,
                request,
            ),
            status=status.HTTP_200_OK,
        )

    def patch(self, request, pk):
        user = self.get_object(pk)

        if user is None:
            return Response(
                {
                    "detail":
                        "User not found."
                },
                status=(
                    status.HTTP_404_NOT_FOUND
                ),
            )

        serializer = UserSerializer(
            user,
            data=request.data,
            partial=True,
            context={
                "request": request
            },
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = serializer.save()

        return Response(
            serialize_user(
                user,
                request,
            ),
            status=status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        user = self.get_object(pk)

        if user is None:
            return Response(
                {
                    "detail":
                        "User not found."
                },
                status=(
                    status.HTTP_404_NOT_FOUND
                ),
            )

        if user.pk == request.user.pk:
            return Response(
                {
                    "detail": (
                        "You cannot delete your "
                        "own logged-in account."
                    )
                },
                status=(
                    status.HTTP_409_CONFLICT
                ),
            )

        if user.profile_image:
            user.profile_image.delete(
                save=False
            )

        user.delete()

        return Response(
            status=(
                status.HTTP_204_NO_CONTENT
            )
        )


class ProfileView(APIView):
    authentication_classes = [
        JWTAuthentication
    ]

    permission_classes = [
        permissions.IsAuthenticated
    ]

    parser_classes = [
        MultiPartParser,
        FormParser,
        JSONParser,
    ]

    def get(self, request):
        return Response(
            serialize_user(
                request.user,
                request,
            ),
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        mutable_data = request.data.copy()

        # A normal user must not change
        # authorization fields from Topbar.
        for protected_field in [
            "email",
            "role",
            "is_active",
            "is_staff",
        ]:
            mutable_data.pop(
                protected_field,
                None,
            )

        serializer = UserSerializer(
            request.user,
            data=mutable_data,
            partial=True,
            context={
                "request": request
            },
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = serializer.save()

        return Response(
            serialize_user(
                user,
                request,
            ),
            status=status.HTTP_200_OK,
        )