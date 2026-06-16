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
from django.conf import settings
from django.conf.urls.static import static

from users.views import CreateUserView, LoginView, UpdateUserView
from .views import home
from django.urls import include, path
from users.views import UploadProfileImageView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path('', home),
    path('admin/', admin.site.urls),
    path("api/auth/login/", LoginView.as_view(), name="auth-login"),
    path("create-user/", CreateUserView.as_view(), name="create-user"),
    path("update-user/<int:user_id>/", UpdateUserView.as_view(), name="update-user"),
    path("api/users/<int:user_id>/upload-profile/", UploadProfileImageView.as_view(), name="upload-profile"),
    path("api/auth/", include("users.urls")),

    path('api/roles/', include('roles.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/components/', include('components.urls')),
    path('api/vendors/', include('vendors.urls')),
    path('api/bom/', include('bom.urls')),
    path('api/procurement/', include('procurement.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/', include('finance.urls')),

]

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
