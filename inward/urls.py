from rest_framework.routers import DefaultRouter
from .views import InwardEntryViewSet

router = DefaultRouter()
router.register('', InwardEntryViewSet, basename='inward')

urlpatterns = router.urls
