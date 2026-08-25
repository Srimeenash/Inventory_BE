from django.urls import path

from rest_framework_simplejwt.views import (
    TokenRefreshView,
)

from .views import (
    LoginResendView,
    LoginVerifyView,
    LoginView,
    ProfileView,
    RegisterView,
    SwitchRoleView,
    UserDetailView,
    UserListCreateView,
)


urlpatterns = [

    # ========================================================
    # LOGIN
    # ========================================================

    path(
        "login/",
        LoginView.as_view(),
        name="auth-login",
    ),

    path(
        "login/verify/",
        LoginVerifyView.as_view(),
        name="auth-login-verify",
    ),

    path(
        "login/resend/",
        LoginResendView.as_view(),
        name="auth-login-resend",
    ),


    # ========================================================
    # JWT REFRESH
    # ========================================================

    path(
        "token/refresh/",
        TokenRefreshView.as_view(),
        name="auth-token-refresh",
    ),


    # ========================================================
    # MULTI-ROLE SWITCH
    # ========================================================
    #
    # Example:
    #
    # POST /auth/switch-role/
    #
    # {
    #     "role": "inventory"
    # }
    #
    # The backend verifies that the requested role belongs
    # to the logged-in user and then returns a new JWT with
    # that role set as active_role.
    #
    # ========================================================

    path(
        "switch-role/",
        SwitchRoleView.as_view(),
        name="auth-switch-role",
    ),


    # ========================================================
    # REGISTER
    # ========================================================

    path(
        "register/",
        RegisterView.as_view(),
        name="auth-register",
    ),


    # ========================================================
    # USER MANAGEMENT
    # ========================================================

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


    # ========================================================
    # CURRENT USER PROFILE
    # ========================================================

    path(
        "profile/",
        ProfileView.as_view(),
        name="auth-profile",
    ),


    # ========================================================
    # EXISTING FRONTEND COMPATIBILITY
    # ========================================================

    path(
        "login/<int:pk>/",
        UserDetailView.as_view(),
        name="auth-login-detail",
    ),
]