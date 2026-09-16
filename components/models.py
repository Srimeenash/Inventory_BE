from django.db import models

class Component(models.Model):
    CATEGORY_CHOICES = [
        ("ACCESSORIES", "Accessories"),
        ("AIRFRAMES", "Airframes"),
        ("COMMUNICATION", "Communication"),
        ("ELECTRICALS", "Electricals"),
        ("ELECTRONICS", "Electronics"),
        ("PAYLOAD", "Payload"),
        ("TOOLS", "Tools"),
    ]

    component_id = models.CharField(
        max_length=100,
        unique=True,
    )

    # Legacy storage retained for historical transaction compatibility.
    # New component creation no longer requires or exposes this field.
    name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    version = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    category = models.CharField(
        max_length=50,
        choices=CATEGORY_CHOICES,
    )
    # NEW - free-text classification entered by Inventory
    component_type = models.CharField(
        max_length=150,
        blank=True,
        null=True,
    )
    specifications = models.TextField(
        blank=True,
        null=True,
    )

    unit_of_measurements = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    hsn_numbers = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    sku_numbers = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    part_numbers = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    product_link = models.URLField(
        blank=True,
        null=True,
    )

    ordering_id = models.IntegerField(
        blank=True,
        null=True,
    )

    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
    )

    tally_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    stock_quantity = models.PositiveIntegerField(
        default=0,
    )

    reorder_level = models.PositiveIntegerField(
        default=5,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    @property
    def total_value(self):
        return self.unit_price * self.stock_quantity

    def __str__(self):
        if self.version:
            return (
                f"{self.component_id} - "
                f"{self.name or self.version} - {self.version}"
            )

        return f"{self.component_id} - {self.name or ''}".rstrip(" -")