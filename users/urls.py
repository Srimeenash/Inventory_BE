from django.urls import path
from rest_framework_simplejwt.views import (
    TokenRefreshView,
)

from .views import (
    LoginView,
    ProfileView,
    RegisterView,
    UserDetailView,
    UserListCreateView,
)


urlpatterns = [
    path(
        "login/",
        LoginView.as_view(),
        name="auth-login",
    ),
    path(
        "token/refresh/",
        TokenRefreshView.as_view(),
        name="auth-token-refresh",
    ),
    path(
        "register/",
        RegisterView.as_view(),
        name="auth-register",
    ),
    path(
        "users/",
        UserListCreateView.as_view(),
        name="auth-user-list-create",
    ),
    path(
        "users/<int:pk>/",
        UserDetailView.as_view(),
        name="auth-user-detail",
    ),
    path(
        "profile/",
        ProfileView.as_view(),
        name="auth-profile",
    ),

    # Existing frontend compatibility.
    path(
        "login/<int:pk>/",
        UserDetailView.as_view(),
        name="auth-login-detail",
    ),
]