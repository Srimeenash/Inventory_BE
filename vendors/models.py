# from django.db import models


# class Vendor(models.Model):
#     name = models.CharField(max_length=255)
#     gst_number = models.CharField(max_length=50, blank=True, null=True)
#     pan_number = models.CharField(max_length=50, blank=True, null=True)
#     contact_person = models.CharField(max_length=255, blank=True, null=True)
#     email = models.EmailField(blank=True, null=True)
#     phone = models.CharField(max_length=30, blank=True, null=True)
#     rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
#     address = models.TextField(blank=True, null=True)
#     is_active = models.BooleanField(default=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)
#     product = models.CharField(max_length=255, blank=True, null=True)
#     product_version = models.CharField(max_length=100, blank=True, null=True)

#     def __str__(self):
#         return self.name

from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=255, unique=True)

    gst_number = models.CharField(max_length=50, blank=True, null=True)
    pan_number = models.CharField(max_length=50, blank=True, null=True)

    contact_person = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=30, blank=True, null=True)

    address = models.TextField(blank=True, null=True)

    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    product = models.CharField(max_length=255, blank=True, null=True)
    product_version = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return self.name