from rest_framework import serializers
from .models import Vendor, VendorProduct


class VendorProductSerializer(serializers.ModelSerializer):
    total = serializers.SerializerMethodField()

    class Meta:
        model = VendorProduct
        fields = [
            "id",
            "product",
            "product_version",
            "quantity",
            "unit",
            "price",
            "gst",
            "total",
        ]

    def get_total(self, obj):
        subtotal = obj.quantity * obj.price
        gst_amount = subtotal * obj.gst / 100
        return subtotal + gst_amount


class VendorSerializer(serializers.ModelSerializer):
    products = VendorProductSerializer(many=True)

    class Meta:
        model = Vendor
        fields = [
            "id",
            "name",
            "contact_person",
            "phone",
            "email",
            "gst_number",
            "pan_number",
            "address",
            "rating",
            "is_active",
            "products",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        products_data = validated_data.pop("products", [])

        vendor = Vendor.objects.create(**validated_data)

        for product in products_data:
            VendorProduct.objects.create(
                vendor=vendor,
                **product
            )

        return vendor

    def update(self, instance, validated_data):
        products_data = validated_data.pop("products", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if products_data is not None:
            instance.products.all().delete()

            for product in products_data:
                VendorProduct.objects.create(
                    vendor=instance,
                    **product
                )

        return instance