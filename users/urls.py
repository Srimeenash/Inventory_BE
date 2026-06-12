from django.urls import path
from .views import LoginView, CreateUserView

urlpatterns = [
    path("api/auth/login/", LoginView.as_view(), name="auth-login"),
      path("create-user/", CreateUserView.as_view(), name="create-user")
]