from rest_framework.routers import DefaultRouter

from .views import (
    InventoryViewSet,
    ProjectInventoryViewSet,
)


router = DefaultRouter()

router.register(
    r"inventory",
    InventoryViewSet,
    basename="inventory",
)

router.register(
    r"project-inventory",
    ProjectInventoryViewSet,
    basename="project-inventory",
)

urlpatterns = router.urls