from django.urls import path
from .views import CreateUserView, UpdateUserView

urlpatterns = [
    path("create-user/", CreateUserView.as_view(), name="create-user"),
    path("update-user/<int:user_id>/", UpdateUserView.as_view(), name="update-user"),
]