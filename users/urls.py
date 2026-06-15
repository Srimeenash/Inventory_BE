from django.urls import path
from .views import LoginView, CreateUserView, UpdateUserView

urlpatterns = [
    path("api/auth/login/", LoginView.as_view(), name="auth-login"),
    path("create-user/", CreateUserView.as_view(), name="create-user"),
    path("update-user/<int:user_id>/", UpdateUserView.as_view(), name="update-user"),
]