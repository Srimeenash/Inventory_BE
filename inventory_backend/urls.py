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
from django.urls import include, path
from .views import home
from users.views import AuthView

urlpatterns = [
    path('', home),
    path('admin/', admin.site.urls),

    # AUTH API
    path('api/auth/', AuthView.as_view()),   # ✅ ADD THIS

    # other modules
    path('api/projects/', include('projects.urls')),
    path('api/components/', include('components.urls')),
    path('api/vendors/', include('vendors.urls')),
    path('api/bom/', include('bom.urls')),
    path('api/procurement/', include('procurement.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/', include('finance.urls')),
    path("api/materialrequest/", include("materialrequest.urls")),
    path("api/component-usage/", include("componentusage.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)