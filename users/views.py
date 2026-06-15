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
        
        

class CreateUserView(APIView):

    def post(self, request):
        data = request.data

        employee_name = data.get("employee_name")
        email = data.get("email")
        password = data.get("password")
        role = data.get("role")
        designation = data.get("designation")
        profile_image = request.FILES.get("profile_image")  # ✅ ADD THIS

        if not all([employee_name, email, password]):
            return Response(
                {"detail": "employee_name, email and password are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "User with this email already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.create(
            employee_name=employee_name,
            email=email,
            password=password,
            role=role,
            designation=designation,
            profile_image=profile_image  # ✅ SAVE IMAGE HERE
        )

        return Response({
            "message": "User created successfully",
            "user": {
                "id": user.id,
                "employee_name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "designation": user.designation,
                "profile_image": user.profile_image.url if user.profile_image else None
            }
        }, status=status.HTTP_201_CREATED)
        
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
        role = data.get("role")
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

        if role:
            user.role = role

        if designation:
            user.designation = designation

        if password:
            user.password = password

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