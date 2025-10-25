from pathlib import Path
from io import BytesIO
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db.models.functions import Lower
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.db.models import F, Avg, Count
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils.text import slugify
from django.utils import timezone
from django.urls import reverse
from django.utils.safestring import mark_safe
from PIL import Image
try:
    import markdown  # pip install markdown
except Exception:
    markdown = None
# ─────── User model ───────
class UserManager(BaseUserManager):
    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email       = models.EmailField(unique=True, db_index=True)
    first_name  = models.CharField(max_length=150, blank=True)
    last_name   = models.CharField(max_length=150, blank=True)
    is_staff    = models.BooleanField(default=False)
    is_active   = models.BooleanField(default=True)
    is_vendor   = models.BooleanField(default=False)  # flag for vendor users
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD  = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email

# ─────── helpers ───────
def _path(instance, filename, prefix: str) -> str:
    return Path(prefix, str(instance.pk or "tmp"), filename).as_posix()

def cat_upload(instance, filename):
    return _path(instance, filename, "categories")

def prod_upload(instance, filename):
    return _path(instance, filename, "products")

def offer_upload(instance, filename):
    return _path(instance, filename, "offers")

def promo_upload(instance, filename):
    return _path(instance, filename, "promo_banners")

def store_upload(instance, filename):
    return _path(instance, filename, "stores")

def blog_upload(instance, filename):
    return _path(instance, filename, "blog")

def resume_upload(instance, filename):
    return _path(instance, filename, "resumes")

def _webp_name_from(file_obj) -> str:
    stem = Path(getattr(file_obj, "name", "image")).stem or "image"
    return f"{stem}.webp"

def compress_to_webp(file_obj, max_kb=150, quality_start=90):
    img = Image.open(file_obj)
    if img.mode != "RGB":
        img = img.convert("RGB")

    q = quality_start
    best = None
    while q >= 20:
        buf = BytesIO()
        img.save(buf, "WEBP", quality=q, method=6)
        size_kb = buf.tell() / 1024
        if size_kb <= max_kb:
            buf.seek(0)
            return ContentFile(buf.read(), name=_webp_name_from(file_obj))
        best = buf
        q -= 10

    if best is not None:
        best.seek(0)
        return ContentFile(best.read(), name=_webp_name_from(file_obj))
    try:
        file_obj.seek(0)
    except Exception:
        pass
    return ContentFile(file_obj.read(), name=_webp_name_from(file_obj))

class TimeStampedMixin(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True

# ───── Grocery / Vendor support ─────
UNIT_CHOICES = (
    ("PCS", "Pieces"),
    ("G",   "Grams"),
    ("KG",  "Kilograms"),
    ("ML",  "Milliliters"),
    ("L",   "Liters"),
    ("BUNDLE", "Bundle / Pack"),
)

class Store(TimeStampedMixin):
    name  = models.CharField(max_length=160, unique=True)
    slug  = models.SlugField(unique=True, blank=True)
    logo  = models.ImageField(upload_to=store_upload, null=True, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address1 = models.CharField(max_length=200, blank=True)
    address2 = models.CharField(max_length=200, blank=True)
    city     = models.CharField(max_length=80, blank=True)
    state    = models.CharField(max_length=80, blank=True)
    postcode = models.CharField(max_length=20, blank=True)
    country  = models.CharField(max_length=60, default="India")
    is_active= models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def save(self, *a, **kw):
        if not self.slug:
            self.slug = slugify(self.name) or f"store-{self.pk or ''}"
        super().save(*a, **kw)
        if self.logo and not str(self.logo.name).lower().endswith(".webp"):
            self.logo = compress_to_webp(self.logo)
            super().save(update_fields=["logo"])

    def __str__(self): return self.name

class Vendor(TimeStampedMixin):
    user         = models.OneToOneField("User", on_delete=models.CASCADE, related_name="vendor")
    display_name = models.CharField(max_length=160)
    store        = models.ForeignKey(Store, null=True, blank=True, related_name="vendors", on_delete=models.SET_NULL)
    is_active    = models.BooleanField(default=True)
    total_units_sold = models.PositiveIntegerField(default=0)
    total_revenue    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    def __str__(self): return self.display_name

class Color(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    hex  = models.CharField(max_length=7, blank=True)
    def save(self, *a, **kw):
        if not self.slug:
            self.slug = slugify(self.name) or f"color-{self.pk or ''}"
        super().save(*a, **kw)
    def __str__(self): return self.name

# ─────── Gold Price Cache (AED/gram) ───────
class GoldPriceSnapshot(TimeStampedMixin):
    source = models.CharField(max_length=50, default="manual")
    price_aed_per_g = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.price_aed_per_g} AED/g @ {self.created_at:%Y-%m-%d %H:%M} ({self.source})"

def get_current_gold_price_aed_per_g(max_age_minutes: int = 60) -> Decimal:
    latest = GoldPriceSnapshot.objects.first()
    if latest and latest.created_at >= timezone.now() - timedelta(minutes=max_age_minutes):
        return Decimal(latest.price_aed_per_g)
    try:
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)
                prompt = (
                    "Give the current spot gold price per gram in AED (United Arab Emirates Dirham). "
                    "Respond with ONLY the number, e.g., 274.12"
                )
                rsp = client.responses.create(
                    model=getattr(settings, "OPENAI_PRICE_MODEL", "gpt-4o-mini"),
                    input=prompt,
                )
                text = (rsp.output_text or "").strip()
                number = "".join(ch for ch in text if (ch.isdigit() or ch == "." ))
                value = Decimal(number)
                GoldPriceSnapshot.objects.create(source="openai", price_aed_per_g=value)
                return value
            except Exception:
                pass
    except Exception:
        pass
    if latest:
        return Decimal(latest.price_aed_per_g)
    return Decimal("250.00")  # fallback

# ─────── Category ───────
class Category(TimeStampedMixin):
    name   = models.CharField(max_length=120)
    slug   = models.SlugField(unique=True, blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, related_name="children", on_delete=models.CASCADE)
    image  = models.ImageField(upload_to=cat_upload, blank=True, null=True)
    icon   = models.CharField(max_length=60, blank=True)

    class Meta:
        verbose_name_plural = "categories"
        indexes = [models.Index(fields=["slug"])]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)
            self.slug = base or f"cat-{self.pk or ''}"
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])

    def __str__(self):
        names = [self.name]
        p = self.parent
        while p:
            names.insert(0, p.name)
            p = p.parent
        return " / ".join(names)

# ─────── Product & ProductImage ───────
class Product(TimeStampedMixin):
    category         = models.ForeignKey(Category, related_name="products", on_delete=models.PROTECT)
    name             = models.CharField(max_length=160, db_index=True)
    slug             = models.SlugField(unique=True, blank=True)

    # Ownership / store
    vendor           = models.ForeignKey(Vendor, null=True, blank=True, related_name="products", on_delete=models.SET_NULL)
    store            = models.ForeignKey(Store,  null=True, blank=True, related_name="products", on_delete=models.SET_NULL)

    quantity         = models.PositiveIntegerField(default=0)
    grade            = models.CharField(max_length=50, blank=True)
    manufacture_date = models.DateField(null=True, blank=True)
    origin_country   = models.CharField(max_length=60, blank=True, default="IN")
    warranty_months  = models.PositiveIntegerField(default=0)

    # Country-specific base prices
    price_inr        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price_usd        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price            = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # legacy fallback

    # AED pricing control
    AED_MODE_CHOICES = (("STATIC", "Static"), ("GOLD", "Gold-Linked"))
    aed_pricing_mode   = models.CharField(max_length=12, choices=AED_MODE_CHOICES, default="STATIC")
    price_aed_static   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # GOLD mode
    gold_weight_g = models.DecimalField(max_digits=10, decimal_places=3, default=0, null=True, blank=True)
    gold_making_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0, null=True, blank=True)
    gold_markup_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, null=True, blank=True)

    discount_percent = models.PositiveIntegerField(default=0)

    in_stock      = models.BooleanField(default=True)
    featured      = models.BooleanField(default=False)
    new_arrival   = models.BooleanField(default=False)
    limited_stock = models.BooleanField(default=False)

    # Description (allow HTML to be pasted)
    description      = models.TextField(blank=True)

    # Nutrition / food info
    ingredients      = models.TextField(blank=True)           # free text
    allergens        = models.CharField(max_length=200, blank=True)  # e.g. "Peanuts, Soy"
    nutrition_facts  = models.JSONField(default=dict, blank=True)    # {"calories":"120kcal", "protein":"3g",...}
    nutrition_notes  = models.TextField(blank=True)           # any extra notes

    hot_deal         = models.BooleanField(default=False)
    hot_deal_ends_at = models.DateTimeField(null=True, blank=True)

    # Grocery additions
    default_uom      = models.CharField(max_length=10, choices=UNIT_CHOICES, default="KG")
    default_pack_qty = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    is_organic       = models.BooleanField(default=False)
    is_perishable    = models.BooleanField(default=True)
    shelf_life_days  = models.PositiveIntegerField(null=True, blank=True)
    hsn_sac          = models.CharField(max_length=12, blank=True)
    gst_rate         = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    mrp_price        = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost_price       = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Visibility to match React "Published" switch
    is_published     = models.BooleanField(default=True, db_index=True)

    views_count   = models.PositiveIntegerField(default=0)
    carts_count   = models.PositiveIntegerField(default=0)
    sold_count    = models.PositiveIntegerField(default=0)
    reviews_count = models.PositiveIntegerField(default=0)
    rating_avg    = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    wishes_count  = models.PositiveIntegerField(default=0)

    # def save(self, *args, **kwargs):
    #     if not self.slug:
    #         base = slugify(self.name)
    #         self.slug = base or f"prod-{self.pk or ''}"
    #     self.in_stock      = self.quantity > 0
    #     self.limited_stock = 0 < self.quantity < 20
    #     super().save(*args, **kwargs)
    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["-sold_count"]),
            models.Index(fields=["is_published"]),
        ]
        ordering = ("-created_at",)

    # --------- lifecycle ---------
    def save(self, *args, **kwargs):
        # Ensure slug before first save and keep it unique
        if not self.slug:
            base = slugify(self.name) or "product"
            slug = base
            i = 1
            # avoid querying all rows; do existence test on each increment
            while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                i += 1
                slug = f"{base}-{i}"
            self.slug = slug

        # Derive stock flags from quantity
        self.in_stock = (self.quantity or 0) > 0
        self.limited_stock = 0 < (self.quantity or 0) < 20

        super().save(*args, **kwargs)

    # Pricing helpers
    def _price_aed_from_gold(self) -> Decimal:
        spot = get_current_gold_price_aed_per_g()  # AED / g
        base = (Decimal(self.gold_weight_g or 0) * Decimal(spot)) + Decimal(self.gold_making_charge or 0)
        if self.gold_markup_percent:
            base = base * (Decimal(100) + Decimal(self.gold_markup_percent)) / Decimal(100)
        return base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def base_price_for_country(self, country_code: str) -> Decimal:
        cc = (country_code or "").upper()
        if cc == "AE":
            if self.aed_pricing_mode == "GOLD":
                return self._price_aed_from_gold()
            if self.price_aed_static is not None:
                return Decimal(self.price_aed_static)
            return (self.price_inr or self.price or Decimal("0.00"))
        if cc == "US":
            return (self.price_usd or self.price or Decimal("0.00"))
        return (self.price_inr or self.price or Decimal("0.00"))

    def discounted_price_for_country(self, country_code: str) -> Decimal:
        price = self.base_price_for_country(country_code)
        if self.discount_percent:
            frac = (Decimal(100) - Decimal(self.discount_percent)) / Decimal(100)
            price = (price * frac)
        return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def discounted_price(self) -> Decimal:
        return self.discounted_price_for_country("IN")

    # --- helpers for images / description ---
    def primary_image(self):
        """Return primary image if set, else the first image."""
        return self.images.filter(is_primary=True).first() or self.images.first()

    @property
    def primary_image_url(self) -> str:
        img = self.primary_image()
        return img.image.url if img and img.image else ""

    @property
    def description_html(self):
        """Treat description as HTML so <h1>, <h2> render exactly."""
        return mark_safe(self.description or "")

    def get_absolute_url(self):
        return reverse("product-detail", kwargs={"slug": self.slug})

    def __str__(self):
        return self.name

class ProductImage(TimeStampedMixin):
    product    = models.ForeignKey(Product, related_name="images", on_delete=models.CASCADE)
    image      = models.ImageField(upload_to=prod_upload)
    is_primary = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "product images"
        indexes = [models.Index(fields=["product", "is_primary"])]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])
        if self.is_primary and self.product_id:
            ProductImage.objects.filter(product_id=self.product_id).exclude(pk=self.pk).update(is_primary=False)

    def __str__(self):
        return f"Image for {self.product}"

# ─────── Product Specifications (name/value pairs) ───────
class ProductSpecification(TimeStampedMixin):
    product     = models.ForeignKey(Product, related_name="specifications", on_delete=models.CASCADE)
    group       = models.CharField(max_length=80, blank=True)  # e.g., "General", "Nutrition", "Packaging"
    name        = models.CharField(max_length=120)
    value       = models.CharField(max_length=300)
    unit        = models.CharField(max_length=40, blank=True)  # optional unit: "kg", "cm", "%"
    is_highlight= models.BooleanField(default=False)           # show in highlights first
    sort_order  = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "group", "name"]
        unique_together = ("product", "group", "name")

    def __str__(self):
        label = f"{self.name}: {self.value}"
        if self.unit:
            label += f" {self.unit}"
        return label

# ─────── VARIANTS ───────
class ProductOption(models.Model):
    product = models.ForeignKey("Product", related_name="options", on_delete=models.CASCADE)
    name = models.CharField(max_length=50)

    class Meta:
        unique_together = ("product", "name")

    def __str__(self):
        return f"{self.product.name} / {self.name}"

class ProductOptionValue(models.Model):
    option = models.ForeignKey(ProductOption, related_name="values", on_delete=models.CASCADE)
    value  = models.CharField(max_length=80)

    class Meta:
        unique_together = ("option", "value")

    def __str__(self):
        return f"{self.option.name}: {self.value}"

class ProductVariant(TimeStampedMixin):
    product    = models.ForeignKey("Product", related_name="variants", on_delete=models.CASCADE)
    sku        = models.CharField(max_length=64, unique=True)
    attributes = models.JSONField(default=dict, blank=True)
    price_override    = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_override = models.PositiveIntegerField(null=True, blank=True)
    quantity   = models.PositiveIntegerField(default=0)
    is_active  = models.BooleanField(default=True)

    # Grocery/retail additions
    weight_value = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    weight_unit  = models.CharField(max_length=10, choices=UNIT_CHOICES, null=True, blank=True)
    color        = models.ForeignKey(Color, null=True, blank=True, related_name="variants", on_delete=models.SET_NULL)
    mrp          = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    barcode      = models.CharField(max_length=64, blank=True)
    min_order_qty = models.PositiveIntegerField(default=1)
    step_qty      = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["weight_unit"]),
        ]
        # ✅ stop duplicate weight variants per product (case-insensitive unit)
        constraints = [
            models.UniqueConstraint(
                fields=["product", "weight_value", "weight_unit"],
                name="uq_variant_product_weight",
                condition=models.Q(weight_value__isnull=False, weight_unit__isnull=False),
            )
        ]

    def clean(self):
        # normalize casing and basic consistency
        if self.weight_unit:
            self.weight_unit = str(self.weight_unit).upper()
        if self.weight_unit and self.weight_value is None:
            raise ValidationError("weight_value is required when weight_unit is set.")

        # prevent duplicate (product, weight_value, weight_unit)
        if (
            self.product_id
            and self.weight_value is not None
            and self.weight_unit
        ):
            clash = ProductVariant.objects.filter(
                product_id=self.product_id,
                weight_value=self.weight_value,
                weight_unit=self.weight_unit,
            )
            if self.pk:
                clash = clash.exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError("A variant with this weight already exists for this product.")

    def save(self, *args, **kwargs):
        # ensure normalized before persistence
        if self.weight_unit:
            self.weight_unit = str(self.weight_unit).upper()
        if self.sku:
            self.sku = self.sku.strip()
        self.full_clean()
        return super().save(*args, **kwargs)

    def unit_price_for_country(self, country_code: str) -> Decimal:
        base = self.price_override if self.price_override is not None else self.product.base_price_for_country(country_code)
        base = Decimal(base or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        disc = self.discount_override if self.discount_override is not None else self.product.discount_percent
        if disc:
            base = (base * (Decimal(100) - Decimal(disc)) / Decimal(100))
        return base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def unit_price(self):
        return self.unit_price_for_country("IN")

    def grams_equivalent(self) -> Decimal | None:
        if not self.weight_value or not self.weight_unit:
            return None
        v = Decimal(self.weight_value)
        if self.weight_unit == "KG": return v * Decimal("1000")
        if self.weight_unit == "G":  return v
        if self.weight_unit == "L":  return v * Decimal("1000")
        if self.weight_unit == "ML": return v
        return None

    def price_per_kg(self, country_code: str = "IN") -> Decimal | None:
        grams = self.grams_equivalent()
        if not grams or grams == 0:
            return None
        unit = self.unit_price_for_country(country_code)
        return (Decimal(unit) * Decimal("1000") / grams).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def __str__(self):
        attrs = ", ".join(f"{k}={v}" for k, v in (self.attributes or {}).items())
        return f"{self.product.name} [{attrs}]"

class VariantImage(TimeStampedMixin):
    variant = models.ForeignKey(ProductVariant, related_name="images", on_delete=models.CASCADE)
    image   = models.ImageField(upload_to=prod_upload)
    is_primary = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])
        if self.is_primary and self.variant_id:
            VariantImage.objects.filter(variant_id=self.variant_id).exclude(pk=self.pk).update(is_primary=False)

    def __str__(self):
        return f"VariantImage for {self.variant}"

# ─────── Cart / Order ───────
class Cart(TimeStampedMixin):
    user        = models.ForeignKey(User, related_name="carts", on_delete=models.CASCADE)
    checked_out = models.BooleanField(default=False)
    def __str__(self):
        return f"Cart #{self.pk} for {self.user}"

class CartItem(TimeStampedMixin):
    cart     = models.ForeignKey(Cart, related_name="items", on_delete=models.CASCADE)
    product  = models.ForeignKey(Product, on_delete=models.PROTECT)
    variant  = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)

    def unit_price_for_country(self, country_code: str) -> Decimal:
        if self.variant_id:
            return self.variant.unit_price_for_country(country_code)
        return self.product.discounted_price_for_country(country_code)

    @property
    def unit_price(self):
        return self.unit_price_for_country("IN")

    @property
    def line_total(self):
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    class Meta:
        unique_together = ("cart", "product", "variant")

    def __str__(self):
        label = f"{self.quantity} × {self.product}"
        if self.variant_id:
            label += f" [{self.variant.attributes}]"
        return label

from decimal import Decimal
from django.db import models, transaction
from django.db.models import F

# ...other imports...
# from .models import Product, ProductVariant, Vendor, Cart, CartItem, User

class Order(TimeStampedMixin):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("cancelled", "Cancelled"),
    )

    # NEW: shipment workflow separate from payment/order status
    SHIPMENT_STATUS_CHOICES = (
        ("placed", "Placed"),
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("delivered", "Delivered"),
    )

    user   = models.ForeignKey(User, related_name="orders", on_delete=models.CASCADE)
    cart   = models.OneToOneField(Cart, on_delete=models.PROTECT)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    payment_method = models.CharField(max_length=30, blank=True)

    # NEW: shipment status (admin can change without touching payment/order status)
    shipment_status = models.CharField(
        max_length=20,
        choices=SHIPMENT_STATUS_CHOICES,
        default="pending",
    )

    # Pricing context
    country_code = models.CharField(max_length=2, default="IN")
    currency     = models.CharField(max_length=8, default="INR")

    def __str__(self):
        return f"Order #{self.pk} ({self.status})"

    @transaction.atomic
    def confirm_and_decrement_stock(self):
        if self.status != "pending":
            return
        vendor_qty = {}
        vendor_sales = {}

        for item in self.cart.items.select_related("product", "variant", "product__vendor"):
            if item.quantity <= 0:
                continue
            if item.variant_id:
                v = item.variant
                if item.quantity > v.quantity:
                    raise ValueError(f"Insufficient stock for variant {v}")
                v.quantity = v.quantity - item.quantity
                v.save(update_fields=["quantity"])
            else:
                p = item.product
                if item.quantity > p.quantity:
                    raise ValueError(f"Insufficient stock for {p.name}")
                p.quantity = p.quantity - item.quantity
                p.save(update_fields=["quantity", "in_stock", "limited_stock"])

            Product.objects.filter(pk=item.product_id).update(sold_count=F("sold_count") + item.quantity)

            vid = getattr(item.product.vendor, "id", None)
            if vid:
                vendor_qty[vid]   = vendor_qty.get(vid, 0) + int(item.quantity)
                vendor_sales[vid] = vendor_sales.get(vid, Decimal("0")) + item.line_total

        self.status = "confirmed"
        self.save(update_fields=["status"])

        for vid, q in vendor_qty.items():
            Vendor.objects.filter(id=vid).update(
                total_units_sold=F("total_units_sold") + q,
                total_revenue=F("total_revenue") + vendor_sales.get(vid, Decimal("0"))
            )


# ─────── Visits / Contact ───────
class VisitEvent(TimeStampedMixin):
    user        = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.TextField(blank=True)
    method      = models.CharField(max_length=10)
    path        = models.CharField(max_length=512)
    referer     = models.CharField(max_length=512, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["path"]),
        ]

    def __str__(self):
        who = self.user.email if self.user_id else "anon"
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {who} {self.method} {self.path}"

class ContactSubmission(TimeStampedMixin):
    user       = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    name       = models.CharField(max_length=150)
    email      = models.EmailField()
    phone      = models.CharField(max_length=40, blank=True)
    subject    = models.CharField(max_length=200, blank=True)
    message    = models.TextField()

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.TextField(blank=True)
    page_url   = models.CharField(max_length=512, blank=True)

    handled    = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Contact from {self.name} <{self.email}>"

def _product_update_stats_on_review(product: "Product"):
    agg = product.reviews.filter(is_approved=True).aggregate(avg=Avg("rating"), cnt=Count("id"))
    avg = Decimal(str(agg["avg"] or 0)).quantize(Decimal("0.00"))
    product.rating_avg = avg
    product.reviews_count = int(agg["cnt"] or 0)
    product.save(update_fields=["rating_avg", "reviews_count"])

class ProductReview(TimeStampedMixin):
    product     = models.ForeignKey(Product, related_name="reviews", on_delete=models.CASCADE)
    user        = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    # NEW (supports anonymous submit)
    user_name   = models.CharField(max_length=150, blank=True)
    user_email  = models.EmailField(blank=True)

    rating      = models.PositiveSmallIntegerField()
    title       = models.CharField(max_length=200, blank=True)
    body        = models.TextField(blank=True)

    # For moderation toggle in your admin UI:
    is_approved = models.BooleanField(default=False)

    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["product", "created_at"])]
        ordering = ["-created_at"]

    def __str__(self):
        who = self.user.email if self.user_id else (self.user_email or "anon")
        return f"{self.product.name} ★{self.rating} by {who}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        _product_update_stats_on_review(self.product)

class OrderCheckoutDetails(TimeStampedMixin):
    order       = models.OneToOneField("Order", related_name="checkout_details", on_delete=models.CASCADE)
    full_name   = models.CharField(max_length=150)
    email       = models.EmailField(blank=True)
    phone       = models.CharField(max_length=40, blank=True)
    address1    = models.CharField(max_length=200)
    address2    = models.CharField(max_length=200, blank=True)
    city        = models.CharField(max_length=80)
    state       = models.CharField(max_length=80, blank=True)
    postcode    = models.CharField(max_length=20, blank=True)
    country     = models.CharField(max_length=60, default="India")
    notes       = models.TextField(blank=True)

    def __str__(self):
        return f"CheckoutDetails for Order #{self.order_id}"

class OrderPayment(TimeStampedMixin):
    METHOD_CHOICES = (
        ("card", "Card"),
        ("bank-transfer", "Bank Transfer"),
        ("cash-on-delivery", "Cash on Delivery"),
    )
    order       = models.OneToOneField("Order", related_name="payment", on_delete=models.CASCADE)
    method      = models.CharField(max_length=30, choices=METHOD_CHOICES)
    provider    = models.CharField(max_length=50, blank=True)
    status      = models.CharField(max_length=50, blank=True)
    transaction_id = models.CharField(max_length=120, blank=True)
    currency    = models.CharField(max_length=12, default="INR")
    amount      = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    raw         = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"Payment for Order #{self.order_id} ({self.method})"

# ─────── Wishlist ───────
class Wishlist(TimeStampedMixin):
    user = models.OneToOneField(User, related_name="wishlist", on_delete=models.CASCADE)

    def __str__(self):
        return f"Wishlist of {self.user.email}"

    @classmethod
    def create_for_user(cls, user):
        obj, created = cls.objects.get_or_create(user=user)
        return obj, created

class WishlistItem(TimeStampedMixin):
    wishlist = models.ForeignKey(Wishlist, related_name="items", on_delete=models.CASCADE)
    product  = models.ForeignKey(Product, related_name="wished_items", on_delete=models.CASCADE)
    variant  = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = ("wishlist", "product", "variant")
        indexes = [models.Index(fields=["wishlist", "product"])]

    def __str__(self):
        base = f"{self.product.name}"
        if self.variant_id:
            base += f" [{self.variant.attributes}]"
        return f"{base} (wish)"

# ─────── Special Offer / Collections / Grid ───────
class SpecialOffer(TimeStampedMixin):
    title       = models.CharField(max_length=120)
    subtitle    = models.CharField(max_length=160, blank=True)
    percentage  = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=200, blank=True)
    image       = models.ImageField(upload_to=offer_upload, blank=True, null=True)
    badge       = models.CharField(max_length=60, blank=True, default="LIMITED TIME")
    cta_label   = models.CharField(max_length=40, blank=True, default="Shop Now")
    cta_url     = models.CharField(max_length=300, blank=True)
    query_params = models.JSONField(default=dict, blank=True)
    starts_at   = models.DateTimeField(null=True, blank=True)
    ends_at     = models.DateTimeField(null=True, blank=True)
    is_active   = models.BooleanField(default=True)
    sort_order  = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "-created_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])

    def __str__(self):
        return f"{self.title} ({self.percentage}%)"

class ProductCollection(TimeStampedMixin):
    name        = models.CharField(max_length=160)
    slug        = models.SlugField(unique=True, blank=True)
    description = models.TextField(blank=True)
    query       = models.JSONField(default=dict, blank=True)
    products    = models.ManyToManyField("Product", blank=True)
    default_limit   = models.PositiveIntegerField(default=12)
    default_order   = models.CharField(max_length=40, default="-created_at")
    is_active   = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name) or f"col-{self.pk or ''}"
        super().save(*args, **kwargs)

    def get_queryset(self):
        if self.products.exists():
            return self.products.all()
        qs = Product.objects.select_related("category").prefetch_related("images")
        q = self.query or {}
        if q.get("featured"):
            qs = qs.filter(featured=True)
        if q.get("hot_deal"):
            qs = qs.filter(hot_deal=True)
        if q.get("new_arrival"):
            qs = qs.filter(new_arrival=True)
        if "category_id" in q:
            qs = qs.filter(category_id=q["category_id"])
        if "category_slug" in q:
            qs = qs.filter(category__slug=q["category_slug"])
        if "min_price" in q:
            qs = qs.filter(price__gte=q["min_price"])
        if "max_price" in q:
            qs = qs.filter(price__lte=q["max_price"])
        return qs

    def __str__(self):
        return self.name

class ProductGrid(TimeStampedMixin):
    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=200, blank=True)
    price_text = models.CharField(max_length=60, blank=True)
    original_price_text = models.CharField(max_length=60, blank=True)
    badge = models.CharField(max_length=80, blank=True)
    discount_text = models.CharField(max_length=60, blank=True)
    image = models.ImageField(upload_to=prod_upload, blank=True, null=True)
    image_url = models.URLField(blank=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="grid_items")
    sort = models.PositiveIntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])

    def __str__(self):
        return self.title

# ─────── Promo Banners ───────
class PromoBanner(TimeStampedMixin):
    PLACEMENTS = (("top", "Top"), ("bottom", "Bottom"))
    VARIANTS = (("default", "Default"), ("coupon", "Coupon"), ("clearance", "Clearance"))

    placement    = models.CharField(max_length=10, choices=PLACEMENTS, default="top", db_index=True)
    variant      = models.CharField(max_length=20, choices=VARIANTS, default="default", db_index=True)

    title        = models.CharField(max_length=120)
    subtitle     = models.CharField(max_length=200, blank=True)
    badge        = models.CharField(max_length=80, blank=True)
    button_text  = models.CharField(max_length=40, blank=True, default="Shop Now")
    cta_url      = models.CharField(max_length=300, blank=True)

    image        = models.ImageField(upload_to=promo_upload, blank=True, null=True)
    image_url    = models.URLField(blank=True)
    class_name   = models.CharField(max_length=300, blank=True)
    overlay_class= models.CharField(max_length=300, blank=True)
    is_wide      = models.BooleanField(default=False)

    coupon_code  = models.CharField(max_length=40, blank=True)
    coupon_text  = models.CharField(max_length=160, blank=True)
    offer_text   = models.CharField(max_length=160, blank=True)
    main_offer   = models.CharField(max_length=160, blank=True)

    is_active    = models.BooleanField(default=True)
    starts_at    = models.DateTimeField(null=True, blank=True)
    ends_at      = models.DateTimeField(null=True, blank=True)
    sort         = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ["sort", "-created_at"]
        indexes = [
            models.Index(fields=["placement", "variant"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["starts_at", "ends_at"]),
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.image and not str(self.image.name).lower().endswith(".webp"):
            self.image = compress_to_webp(self.image)
            super().save(update_fields=["image"])

    def __str__(self):
        return f"{self.placement}/{self.variant}: {self.title}"

# ─────── Blog ───────


class BlogCategory(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="blog/categories/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)


class BlogPost(models.Model):
    category = models.ForeignKey(BlogCategory, on_delete=models.SET_NULL, null=True, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="blog_posts")

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=240, unique=True, blank=True)

    excerpt = models.TextField(blank=True)

    # Authoring fields
    content_markdown = models.TextField(blank=True)  # <- you edit in markdown
    content_html = models.TextField(blank=True)      # <- auto-rendered from markdown for fast reads

    cover = models.ImageField(upload_to="blog/covers/", blank=True, null=True)

    # lightweight tags CSV to avoid DB-specific ArrayField
    tags_csv = models.CharField(max_length=600, blank=True, help_text="Comma separated tags")

    featured = models.BooleanField(default=False)
    is_published = models.BooleanField(default=True)
    published_at = models.DateTimeField(default=timezone.now)

    views_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_published", "published_at"]),
        ]

    def __str__(self):
        return self.title

    @property
    def tags(self) -> list[str]:
        return [t.strip() for t in (self.tags_csv or "").split(",") if t.strip()]

    def set_tags(self, tags_list: list[str]):
        self.tags_csv = ", ".join(sorted({t.strip() for t in tags_list if t and t.strip()}))

    def save(self, *args, **kwargs):
        # slug
        if not self.slug:
            base = slugify(self.title)[:220]
            slug = base or f"post-{int(timezone.now().timestamp())}"
            # ensure unique
            idx = 1
            unique = slug
            while BlogPost.objects.exclude(pk=self.pk).filter(slug=unique).exists():
                idx += 1
                unique = f"{base}-{idx}"[:240]
            self.slug = unique

        # render markdown -> html
        if self.content_markdown and markdown:
            self.content_html = markdown.markdown(
                self.content_markdown,
                extensions=["extra", "codehilite", "toc", "sane_lists"],
            )

        super().save(*args, **kwargs)


class BlogPostVersion(models.Model):
    """Immutable snapshot saved on every post create/update."""
    post = models.ForeignKey(BlogPost, related_name="versions", on_delete=models.CASCADE)
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    excerpt = models.TextField(blank=True)
    content_markdown = models.TextField(blank=True)
    content_html = models.TextField(blank=True)
    tags_csv = models.CharField(max_length=600, blank=True)
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="blog_edits")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("post", "version")]
        ordering = ["-version", "-created_at"]

    def __str__(self):
        return f"{self.post.slug} v{self.version}"

# ─────── Careers ───────
class JobOpening(TimeStampedMixin):
    EMPLOYMENT_TYPES = (("full-time","Full-time"), ("part-time","Part-time"), ("contract","Contract"), ("intern","Intern"))

    title       = models.CharField(max_length=160)
    department  = models.CharField(max_length=120, blank=True)
    location    = models.CharField(max_length=120, blank=True)
    employment_type = models.CharField(max_length=20, choices=EMPLOYMENT_TYPES, default="full-time")
    salary_text = models.CharField(max_length=120, blank=True)

    description = models.TextField()
    requirements = models.JSONField(default=list, blank=True)
    is_active   = models.BooleanField(default=True)
    posted_at   = models.DateTimeField(default=timezone.now)

    apply_email = models.EmailField(blank=True)
    apply_url   = models.URLField(blank=True)

    class Meta:
        ordering = ["-posted_at", "-created_at"]
        indexes = [models.Index(fields=["is_active", "posted_at"])]

    def __str__(self):
        return f"{self.title} ({self.location or 'Remote'})"

class JobApplication(TimeStampedMixin):
    STATUS = (("submitted","Submitted"), ("reviewed","Reviewed"), ("rejected","Rejected"), ("hired","Hired"))

    job         = models.ForeignKey(JobOpening, related_name="applications", on_delete=models.CASCADE)
    full_name   = models.CharField(max_length=150)
    email       = models.EmailField()
    phone       = models.CharField(max_length=40, blank=True)
    cover_letter = models.TextField(blank=True)
    resume      = models.FileField(upload_to=resume_upload, null=True, blank=True)

    status      = models.CharField(max_length=20, choices=STATUS, default="submitted", db_index=True)
    notes       = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["job", "status"])]

    def __str__(self):
        return f"Application: {self.full_name} → {self.job.title}"




def _upload_testimonials(instance, filename):
    return f"testimonials/{filename}"

def _upload_videos(instance, filename):
    return f"videos/{filename}"

def _upload_awards(instance, filename):
    return f"awards/{filename}"

def _upload_gallery(instance, filename):
    return f"gallery/{filename}"


class Testimonial(models.Model):
    """Customer testimonials powering your Testimonials page."""
    name        = models.CharField(max_length=150)
    location    = models.CharField(max_length=150, blank=True)
    rating      = models.PositiveSmallIntegerField(default=5)
    testimonial = models.TextField()
    product     = models.CharField(max_length=150, blank=True)
    avatar      = models.ImageField(upload_to=_upload_testimonials, blank=True, null=True)
    verified    = models.BooleanField(default=True)
    is_active   = models.BooleanField(default=True)
    sort        = models.PositiveIntegerField(default=0, db_index=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort", "-created_at"]
        indexes = [
            models.Index(fields=["is_active", "sort"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.location})"


class VideoTestimonial(models.Model):
    """Short video cards (name/desc/thumbnail/duration)."""
    name        = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    thumbnail   = models.ImageField(upload_to=_upload_videos)
    # Optional: if you later host the video file or an external URL
    video_file  = models.FileField(upload_to=_upload_videos, blank=True, null=True)
    video_url   = models.URLField(blank=True)
    duration    = models.CharField(max_length=16, blank=True)  # "2:34"

    is_active   = models.BooleanField(default=True)
    sort        = models.PositiveIntegerField(default=0, db_index=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort", "-created_at"]
        indexes = [
            models.Index(fields=["is_active", "sort"]),
        ]

    def __str__(self):
        return self.name


class AwardRecognition(models.Model):
    """Awards & Recognition cards."""
    CATEGORY_CHOICES = [
        ("Industry Recognition", "Industry Recognition"),
        ("Sustainability", "Sustainability"),
        ("Social Impact", "Social Impact"),
        ("Customer Excellence", "Customer Excellence"),
        ("Innovation", "Innovation"),
        ("Quality", "Quality"),
    ]

    title        = models.CharField(max_length=200)
    organization = models.CharField(max_length=200, blank=True)
    year         = models.CharField(max_length=10, blank=True)  # keep as text to allow "2022-23"
    description  = models.TextField(blank=True)
    category     = models.CharField(max_length=64, choices=CATEGORY_CHOICES, default="Industry Recognition")
    emblem       = models.ImageField(upload_to=_upload_awards, blank=True, null=True)

    is_active    = models.BooleanField(default=True)
    sort         = models.PositiveIntegerField(default=0, db_index=True)

    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort", "-created_at"]
        indexes = [models.Index(fields=["is_active", "sort"])]

    def __str__(self):
        return f"{self.title} ({self.year})"


class Certification(models.Model):
    """Certifications & Licenses list."""
    name        = models.CharField(max_length=200)
    authority   = models.CharField(max_length=200, blank=True)
    valid_until = models.CharField(max_length=20, blank=True)  # "2025"
    description = models.TextField(blank=True)

    is_active   = models.BooleanField(default=True)
    sort        = models.PositiveIntegerField(default=0, db_index=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort", "-created_at"]
        indexes = [models.Index(fields=["is_active", "sort"])]

    def __str__(self):
        return self.name


class GalleryItem(models.Model):
    """Flexible gallery items (Farming, Events, Certifications, Community)."""
    CATEGORY_CHOICES = [
        ("Farming & Agriculture", "Farming & Agriculture"),
        ("Events & Workshops", "Events & Workshops"),
        ("Certifications", "Certifications"),
        ("Community Impact", "Community Impact"),
    ]

    category     = models.CharField(max_length=64, choices=CATEGORY_CHOICES)
    image        = models.ImageField(upload_to=_upload_gallery)
    title        = models.CharField(max_length=200)
    location     = models.CharField(max_length=200, blank=True)
    date_label   = models.CharField(max_length=64, blank=True)  # "March 2024"
    description  = models.TextField(blank=True)
    attendees    = models.CharField(max_length=64, blank=True)  # e.g. "200+ Farmers" for Events

    is_active    = models.BooleanField(default=True)
    sort         = models.PositiveIntegerField(default=0, db_index=True)

    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "sort", "-created_at"]
        indexes = [
            models.Index(fields=["category", "is_active", "sort"]),
        ]

    def __str__(self):
        return f"[{self.category}] {self.title}"
