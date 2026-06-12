"""
URL configuration for inventory_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin

from users.views import LoginView
from .views import home
from django.urls import include, path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path('', home),
    path('admin/', admin.site.urls),
    # path('api/auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    # path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path("api/auth/login/", LoginView.as_view(), name="auth-login"),
    path("api/auth/", include("users.urls")),
    # path('api/accounts/', include('accounts.urls')),
    path('api/roles/', include('roles.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/components/', include('components.urls')),
    path('api/vendors/', include('vendors.urls')),
    path('api/bom/', include('bom.urls')),
    path('api/procurement/', include('procurement.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/', include('finance.urls')),
]
