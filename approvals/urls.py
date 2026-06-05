from rest_framework.routers import DefaultRouter
from .views import ApprovalRequestViewSet

router = DefaultRouter()

router.register('approvals', ApprovalRequestViewSet, basename='approvals')

urlpatterns = router.urls