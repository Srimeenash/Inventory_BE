from django.urls import path
from users.views import AuthView

urlpatterns = [
    path("api/auth/", AuthView.as_view(), name="auth"),
]