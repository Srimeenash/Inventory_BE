from rest_framework.routers import DefaultRouter
from .views import BOMViewSet, BOMItemViewSet, MasterBOMViewSet, ProjectBOMViewSet

router = DefaultRouter()

router.register('bom', BOMViewSet, basename='bom')
router.register('bom-items', BOMItemViewSet, basename='bom-items')
router.register('project-boms', ProjectBOMViewSet, basename='project-boms')
router.register('master-bom', MasterBOMViewSet, basename='master-bom')

urlpatterns = router.urls
