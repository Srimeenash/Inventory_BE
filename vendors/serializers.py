from rest_framework import serializers
from .models import Vendor


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = [
        'id',
        'name',
        'product',
        'product_version',
        'contact_person',
        'phone',
        'gst_number',
        'rating',
        'is_active',
    ]
        read_only_fields = ['id', 'created_at', 'updated_at']
