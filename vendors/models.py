from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=255, unique=True)

    contact_person = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )
    vendor_id = models.CharField(
        max_length=20,
        editable=False,
        blank=True,
        null=True,
    )
    phone_number = models.CharField(
        max_length=30,
        blank=True,
        null=True
    )
    manager = models.CharField(
        max_length=255,
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
    def save(self, *args, **kwargs):
        if not self.vendor_id:
            last_vendor = Vendor.objects.order_by("-id").first()

            if last_vendor and last_vendor.vendor_id:
                try:
                    last_number = int(last_vendor.vendor_id.split("_")[1])
                except (IndexError, ValueError):
                    last_number = 0
            else:
                last_number = 0

            self.vendor_id = f"VEN_{last_number + 1:04d}"

        super().save(*args, **kwargs)

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
        blank=True,
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