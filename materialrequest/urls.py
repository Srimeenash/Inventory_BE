from rest_framework.routers import DefaultRouter
from .views import MaterialRequestViewSet

router = DefaultRouter()
router.register(r'material-requests', MaterialRequestViewSet)

urlpatterns = router.urls
