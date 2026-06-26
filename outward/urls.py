from rest_framework.routers import DefaultRouter
from .views import OutwardEntryViewSet

router = DefaultRouter()
router.register('', OutwardEntryViewSet, basename='outward')

urlpatterns = router.urls
