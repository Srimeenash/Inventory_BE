from rest_framework.routers import DefaultRouter
from .views import ComponentViewSet

router = DefaultRouter()
router.register('components', ComponentViewSet, basename='component')

urlpatterns = router.urls
