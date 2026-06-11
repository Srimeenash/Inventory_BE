from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import User


class LoginView(APIView):

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")

        # validate input
        if not email or not password:
            return Response(
                {"detail": "Email and password required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # check user by email only
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"detail": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # check password
        if user.password != password:
            return Response(
                {"detail": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # success response
        return Response({
            "message": "Login successful",
            "user": {
                "id": user.id,
                "employee_name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "designation": user.designation
            }
        }, status=status.HTTP_200_OK)