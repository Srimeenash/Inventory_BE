from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from roles.models import Role
from users.models import User
from django.contrib.auth import get_user_model

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"detail": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not user.check_password(password):
            return Response(
                {"detail": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "email": user.email,
                "employee_name": user.employee_name,
                "role": user.role if user.role else None,
                "designation": user.designation,
                "profile_image": user.profile_image.url if user.profile_image else None,
            }
        })
class CreateUserView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            data = request.data
            role_name = data.get("role")

            # Validate role if provided
            valid_roles = [choice[0] for choice in User.ROLE_CHOICES]
            if role_name and role_name not in valid_roles:
                return Response({
                    "error": f"Invalid role. Must be one of: {', '.join(valid_roles)}"
                }, status=400)

            user = User(
                employee_name=data.get("employee_name"),
                email=data.get("email"),
                role=role_name,
                designation=data.get("designation"),
            )

            user.set_password(data.get("password"))
            user.save()

            return Response({
                "message": "User created successfully",
                "id": user.id,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "employee_name": user.employee_name,
                    "role": user.role,
                    "designation": user.designation,
                }
            }, status=201)

        except Exception as e:
            return Response({"error": str(e)}, status=500)
        
class UpdateUserView(APIView):

    def put(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"detail": "User not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        data = request.data

        employee_name = data.get("employee_name")
        email = data.get("email")
        role_name = data.get("role")
        designation = data.get("designation")
        password = data.get("password")

        # ✅ IMAGE COMES FROM FILES (IMPORTANT)
        profile_image = request.FILES.get("profile_image")

        # -----------------------
        # UPDATE FIELDS
        # -----------------------
        if employee_name:
            user.employee_name = employee_name

        if email:
            if User.objects.exclude(id=user_id).filter(email=email).exists():
                return Response(
                    {"detail": "Email already exists"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            user.email = email

        if role_name:
            # Validate role
            valid_roles = [choice[0] for choice in User.ROLE_CHOICES]
            if role_name not in valid_roles:
                return Response({
                    "error": f"Invalid role. Must be one of: {', '.join(valid_roles)}"
                }, status=400)
            user.role = role_name

        if designation:
            user.designation = designation

        if password:
            user.set_password(password)

        # ✅ UPDATE IMAGE
        if profile_image:
            user.profile_image = profile_image

        user.save()

        return Response({
            "message": "User updated successfully",
            "user": {
                "id": user.id,
                "employee_name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "designation": user.designation,
                "profile_image": user.profile_image.url if user.profile_image else None
            }
        }, status=status.HTTP_200_OK)
        
        
class UploadProfileImageView(APIView):

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"detail": "User not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if "profile_image" not in request.FILES:
            return Response(
                {"detail": "No image provided"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.profile_image = request.FILES["profile_image"]
        user.save()

        return Response({
            "message": "Profile image uploaded successfully",
            "profile_image": user.profile_image.url if user.profile_image else None
        }, status=status.HTTP_200_OK)