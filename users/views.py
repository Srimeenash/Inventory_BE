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

from .models import User
from .serializers import (
    EmailTokenSerializer,
    UserSerializer,
)


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
        serializer = EmailTokenSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = serializer.validated_data[
            "user"
        ]

        refresh = RefreshToken.for_user(
            user
        )

        return Response(
            {
                "access": str(
                    refresh.access_token
                ),
                "refresh": str(refresh),
                "user": serialize_user(
                    user,
                    request,
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
                status=
                    status.HTTP_404_NOT_FOUND,
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
                status=
                    status.HTTP_404_NOT_FOUND,
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
                status=
                    status.HTTP_404_NOT_FOUND,
            )

        if user.pk == request.user.pk:
            return Response(
                {
                    "detail": (
                        "You cannot delete your "
                        "own logged-in account."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        if user.profile_image:
            user.profile_image.delete(
                save=False
            )

        user.delete()

        return Response(
            status=
                status.HTTP_204_NO_CONTENT
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