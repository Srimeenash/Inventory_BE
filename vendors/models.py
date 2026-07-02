from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=255, unique=True)

    contact_person = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    phone = models.CharField(
        max_length=30,
        blank=True,
        null=True
    )

    email = models.EmailField(
        blank=True,
        null=True
    )

    gst_number = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    pan_number = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    address = models.TextField(
        blank=True,
        null=True
    )

    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0
    )

    is_active = models.BooleanField(
        default=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return self.name


class VendorProduct(models.Model):
    vendor = models.ForeignKey(
        Vendor,
        related_name="products",
        on_delete=models.CASCADE
    )

    product = models.CharField(
        max_length=255
    )

    product_version = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    quantity = models.PositiveIntegerField(
        default=1
    )

    unit = models.CharField(
        max_length=50,
        default="pcs"
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    gst = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    @property
    def total(self):
        subtotal = self.quantity * self.price
        gst_amount = subtotal * self.gst / 100
        return subtotal + gst_amount

    def __str__(self):
        return f"{self.vendor.name} - {self.product}"