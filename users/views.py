from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import User


class AuthView(APIView):

    def post(self, request):
        data = request.data

        email = data.get("email")
        password = data.get("password")
        employee_name = data.get("employee_name")
        role = data.get("role", "inventory")

        if not email or not password:
            return Response(
                {"error": "email and password required"},
                status=400
            )

        # -------------------------
        # TRY LOGIN
        # -------------------------
        try:
            user = User.objects.get(email=email)

            if not user.check_password(password):
                return Response({"error": "Invalid credentials"}, status=401)

        except User.DoesNotExist:
            # -------------------------
            # REGISTER USER
            # -------------------------
            if not employee_name:
                return Response(
                    {"error": "employee_name required for registration"},
                    status=400
                )

            user = User.objects.create(
                email=email,
                employee_name=employee_name,
                role=role,
                is_active=True
            )
            user.set_password(password)
            user.save()

        # -------------------------
        # TOKEN
        # -------------------------
        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),

            "user": {
                "id": user.id,
                "name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
            }
        })