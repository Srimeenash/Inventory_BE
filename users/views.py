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
            designation=designation
        )

        return Response({
            "message": "User created successfully",
            "user": {
                "id": user.id,
                "employee_name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "designation": user.designation
            }
        }, status=status.HTTP_201_CREATED)

    def post(self, request):
        try:
            data = request.data

            employee_name = data.get("employee_name")
            email = data.get("email")
            password = data.get("password")
            role = data.get("role")
            designation = data.get("designation")

            if not employee_name or not email or not password:
                return Response(
                    {"detail": "Required fields missing"},
                    status=400
                )

            if User.objects.filter(email=email).exists():
                return Response(
                    {"detail": "Email already exists"},
                    status=400
                )

            user = User(
                employee_name=employee_name,
                email=email,
                role=role,
                designation=designation
            )

            user.password = password
            user.save()

            return Response({
                "message": "User created",
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role
                }
            }, status=201)

        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=500
            )

    def post(self, request):
        data = request.data

        employee_name = data.get("employee_name")
        email = data.get("email")
        password = data.get("password")
        role = data.get("role")
        designation = data.get("designation")

        # -----------------------
        # VALIDATION
        # -----------------------
        if not all([employee_name, email, password]):
            return Response(
                {"detail": "employee_name, email and password are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # check duplicate email
        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "User with this email already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # -----------------------
        # CREATE USER SAFELY
        # -----------------------
        user = User(
            employee_name=employee_name,
            email=email,
            role=role,
            designation=designation
        )

        # IMPORTANT: hash password properly
        user.password = password
        user.save()

        return Response({
            "message": "User created successfully",
            "user": {
                "id": user.id,
                "employee_name": user.employee_name,
                "email": user.email,
                "role": user.role,
                "designation": user.designation
            }
        }, status=status.HTTP_201_CREATED)