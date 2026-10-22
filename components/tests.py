from django.test import TestCase
from rest_framework.test import APIRequestFactory

from bom.models import BOM, BOMItem

from .models import Component
from .views import ComponentViewSet


class ComponentDeleteTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def delete_component(self, component):
        request = self.factory.delete(
            f"/api/components/components/{component.pk}/"
        )
        view = ComponentViewSet.as_view(
            {"delete": "destroy"}
        )
        return view(request, pk=component.pk)

    def test_unused_component_is_deleted(self):
        component = Component.objects.create(
            component_id="AC_9001",
            category="ACCESSORIES",
        )

        response = self.delete_component(component)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            Component.objects.filter(pk=component.pk).exists()
        )

    def test_component_with_bom_history_is_deactivated(self):
        component = Component.objects.create(
            component_id="AC_9002",
            category="ACCESSORIES",
        )
        bom = BOM.objects.create(
            bom_number="BOM-DELETE-TEST",
            product_name="Delete safety test",
            created_by="test",
        )
        bom_item = BOMItem.objects.create(
            bom=bom,
            component=component,
        )

        response = self.delete_component(component)

        component.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(component.is_active)
        self.assertTrue(
            BOMItem.objects.filter(pk=bom_item.pk).exists()
        )
        self.assertFalse(response.data["deleted"])
