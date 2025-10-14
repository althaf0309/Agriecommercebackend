# serializers.py
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, List, Dict
import json
from django.db import transaction
from rest_framework import serializers

from .models import *

# ---------- Helpers ----------

class IDRelatedField(serializers.PrimaryKeyRelatedField):
    """Accept raw integer id (or null) for FK fields, expose as *_id."""
    def to_representation(self, value):
        return value.pk if value else None


def _to_decimal(val: Any, default: str = "0.00", allow_none: bool = False) -> Optional[Decimal]:
    if val in (None, "", "null"):
        return None if allow_none else Decimal(default)
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default) if not allow_none else None


def _absolute_media_url(request, path_or_file):
    """
    Build absolute URL for ImageField/FileField or path/relative URL.
    Accepts:
      - string URL/path
      - Django File/ImageField (has .url)
      - model with `.image` (e.g., ProductImage)
    """
    if not path_or_file:
        return None

    # ProductImage-like object with `.image`
    if hasattr(path_or_file, "image"):
        path_or_file = getattr(path_or_file, "image")

    # Django file/field with .url (may raise if missing)
    url = None
    try:
        url = getattr(path_or_file, "url", None)
    except Exception:
        url = None

    if not url:
        url = str(path_or_file or "")

    if not url:
        return None

    if url.startswith(("http://", "https://")):
        return url

    from django.conf import settings
    media_prefix = getattr(settings, "MEDIA_URL", "/media/")
    if request is None:
        return url if url.startswith("/") else (media_prefix.rstrip("/") + "/" + url.lstrip("/"))

    base = request.build_absolute_uri("/")
    from urllib.parse import urljoin
    if url.startswith("/"):
        return urljoin(base, url.lstrip("/"))
    return urljoin(base, (media_prefix + url).lstrip("/"))


# ---------- Basic serializers ----------

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "slug", "parent", "icon", "image"]


class StoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ["id", "name", "slug", "email", "phone", "city", "state", "country", "is_active", "logo"]


class VendorSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    store = StoreSerializer(read_only=True)
    store_id = IDRelatedField(source="store", queryset=Store.objects.all(), required=False, allow_null=True)

    class Meta:
        model = Vendor
        fields = [
            "id", "display_name", "user_id",
            "store", "store_id",
            "is_active", "total_units_sold", "total_revenue",
        ]


class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        fields = ["id", "name", "slug", "hex"]


# ---------- Product images / variant images ----------

class ProductImageSerializer(serializers.ModelSerializer):
    # accept raw integer id in multipart/form (write-only), and resolve to FK
    product = serializers.IntegerField(write_only=True)

    class Meta:
        model = ProductImage
        fields = ["id", "product", "image", "is_primary", "created_at"]
        read_only_fields = ["created_at"]

    def create(self, validated_data):
        pid = validated_data.pop("product", None)
        if not pid:
            raise serializers.ValidationError({"product": "This field is required."})
        try:
            product = Product.objects.get(pk=int(pid))
        except (Product.DoesNotExist, ValueError):
            raise serializers.ValidationError({"product": "Invalid product id."})
        validated_data["product"] = product
        return super().create(validated_data)


class VariantImageSerializer(serializers.ModelSerializer):
    # same idea for variant images
    variant = serializers.IntegerField(write_only=True)

    class Meta:
        model = VariantImage
        fields = ["id", "variant", "image", "is_primary", "created_at"]
        read_only_fields = ["created_at"]

    def create(self, validated_data):
        vid = validated_data.pop("variant", None)
        if not vid:
            raise serializers.ValidationError({"variant": "This field is required."})
        try:
            variant = ProductVariant.objects.get(pk=int(vid))
        except (ProductVariant.DoesNotExist, ValueError):
            raise serializers.ValidationError({"variant": "Invalid variant id."})
        validated_data["variant"] = variant
        return super().create(validated_data)


# ---------- Variants ----------

class ProductVariantSerializer(serializers.ModelSerializer):
    color_id = IDRelatedField(source="color", queryset=Color.objects.all(), required=False, allow_null=True)
    price_override = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    mrp = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    weight_value = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id", "product", "sku", "attributes",
            "price_override", "discount_override",
            "quantity", "is_active",
            "weight_value", "weight_unit", "color_id",
            "mrp", "barcode", "min_order_qty", "step_qty",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        attrs["price_override"] = _to_decimal(attrs.get("price_override"), allow_none=True)
        attrs["mrp"] = _to_decimal(attrs.get("mrp"), allow_none=True)
        wv = attrs.get("weight_value")
        attrs["weight_value"] = _to_decimal(wv, allow_none=True)
        return attrs


# ---------- Specifications ----------

class ProductSpecificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductSpecification
        fields = [
            "id", "product", "group", "name", "value", "unit",
            "is_highlight", "sort_order", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ---------- Product (Read) ----------

class ProductReadSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    vendor = VendorSerializer(read_only=True)
    store = StoreSerializer(read_only=True)

    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    specifications = ProductSpecificationSerializer(many=True, read_only=True)

    price_in_country = serializers.SerializerMethodField()
    discounted_price_in_country = serializers.SerializerMethodField()
    primary_image_url = serializers.SerializerMethodField()
    description_html = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "name", "slug", "description",
            "description_html", "primary_image_url",
            "category", "vendor", "store",
            "quantity", "grade", "manufacture_date",
            "origin_country", "warranty_months",
            "price_inr", "price_usd", "price",
            "aed_pricing_mode", "price_aed_static",
            "gold_weight_g", "gold_making_charge", "gold_markup_percent",
            "discount_percent",
            "in_stock", "featured", "new_arrival", "limited_stock",
            "hot_deal", "hot_deal_ends_at",
            "default_uom", "default_pack_qty", "is_organic", "is_perishable",
            "shelf_life_days", "hsn_sac", "gst_rate", "mrp_price", "cost_price",
            "is_published",
            # nutrition
            "ingredients", "allergens", "nutrition_facts", "nutrition_notes",
            # stats
            "views_count", "carts_count", "sold_count", "reviews_count", "rating_avg", "wishes_count",
            # related
            "images", "variants", "specifications",
            # computed prices
            "price_in_country", "discounted_price_in_country",
            "created_at", "updated_at",
        ]

    def _country(self) -> str:
        return (self.context.get("country_code") or "IN").upper()

    def get_price_in_country(self, obj: Product) -> str:
        return f"{obj.base_price_for_country(self._country()):.2f}"

    def get_discounted_price_in_country(self, obj: Product) -> str:
        return f"{obj.discounted_price_for_country(self._country()):.2f}"

    def get_primary_image_url(self, obj: Product) -> str:
        """
        Resolve a correct, absolute primary image URL from multiple shapes:
          - obj.primary_image_url (string)
          - obj.primary_image() method returning ProductImage / File / URL
          - obj.primary_image attribute (ProductImage / File / URL)
          - first related ProductImage (primary/first)
        """
        request = self.context.get("request")

        # 1) explicit url string if present
        url_attr = getattr(obj, "primary_image_url", None)
        if isinstance(url_attr, str) and url_attr.strip():
            return _absolute_media_url(request, url_attr)

        # 2) primary_image (callable or attribute)
        prim = getattr(obj, "primary_image", None)
        try:
            if callable(prim):
                prim = prim()  # call the method
        except Exception:
            prim = None

        if prim:
            return _absolute_media_url(request, prim)

        # 3) related primary
        try:
            primary_rel = next((im for im in getattr(obj, "images", []).all() if getattr(im, "is_primary", False)), None)
        except Exception:
            primary_rel = None
        if primary_rel:
            return _absolute_media_url(request, primary_rel)

        # 4) first related
        try:
            first_rel = getattr(obj, "images", []).first()
        except Exception:
            first_rel = None
        if first_rel:
            return _absolute_media_url(request, first_rel)

        return None

    def get_description_html(self, obj: Product) -> str:
        return str(obj.description_html)


# ---------- Product (Create/Update – V1, non-required) ----------

class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    category_id = IDRelatedField(queryset=Category.objects.all(), source="category", required=False, allow_null=True)
    vendor_id = IDRelatedField(queryset=Vendor.objects.all(), source="vendor", required=False, allow_null=True)
    store_id = IDRelatedField(queryset=Store.objects.all(), source="store", required=False, allow_null=True)

    price_inr = serializers.CharField(required=False, allow_blank=True, default="0.00")
    gst_rate = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="0.00")
    mrp_price = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    cost_price = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    default_pack_qty = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "category_id", "vendor_id", "store_id",
            "name", "description", "origin_country", "grade",
            "quantity", "manufacture_date", "is_perishable", "is_organic", "shelf_life_days",
            "default_uom", "default_pack_qty",
            "price_inr", "discount_percent", "hsn_sac", "gst_rate", "mrp_price", "cost_price",
            "featured", "new_arrival", "hot_deal", "hot_deal_ends_at",
            "warranty_months",
            "is_published",
            # nutrition fields (optional)
            "ingredients", "allergens", "nutrition_facts", "nutrition_notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.required = False

    def validate(self, attrs):
        attrs["price_inr"] = _to_decimal(attrs.get("price_inr"), default="0.00")
        attrs["gst_rate"] = _to_decimal(attrs.get("gst_rate"), allow_none=True)
        attrs["mrp_price"] = _to_decimal(attrs.get("mrp_price"), allow_none=True)
        attrs["cost_price"] = _to_decimal(attrs.get("cost_price"), allow_none=True)
        dpk = attrs.get("default_pack_qty")
        attrs["default_pack_qty"] = _to_decimal(dpk, allow_none=True)
        return attrs

    def create(self, validated_data):
        return Product.objects.create(**validated_data)

    def update(self, instance, validated_data):
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        return instance

    def to_internal_value(self, data):
        # Accept JSON strings when request is multipart
        def _maybe_json(key, default):
            v = data.get(key, default)
            if isinstance(v, (list, dict)) or v is None:
                return v
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    return default
                try:
                    return json.loads(v)
                except Exception:
                    return default
            return default

        # Normalize before DRF validation
        if hasattr(data, "mutable") and not data.mutable:
            data = data.copy()

        data["variants"] = _maybe_json("variants", [])
        data["images_meta"] = _maybe_json("images_meta", [])

        return super().to_internal_value(data)


# ---------- Wishlist ----------

class WishlistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = WishlistItem
        fields = ["id", "product", "variant", "created_at", "updated_at"]


class WishlistSerializer(serializers.ModelSerializer):
    items = WishlistItemSerializer(many=True, read_only=True)
    class Meta:
        model = Wishlist
        fields = ["id", "user", "items", "created_at", "updated_at"]
        read_only_fields = ["user", "items", "created_at", "updated_at"]


class ContactSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactSubmission
        fields = [
            "id", "name", "email", "phone", "subject", "message",
            "handled", "created_at"
        ]
    read_only_fields = ["handled", "created_at"]


class ProductReviewSerializer(serializers.ModelSerializer):
    # Allow frontend to send `comment`; map to `body`
    comment = serializers.CharField(source="body", required=False, allow_blank=True)

    class Meta:
        model = ProductReview
        fields = [
            "id", "product", "user", "user_name", "user_email",
            "rating", "title", "comment", "body",
            "is_approved", "created_at", "updated_at",
        ]
        read_only_fields = ["user", "is_approved", "created_at", "updated_at", "body"]
        extra_kwargs = {
            "body": {"required": False, "allow_blank": True},
            "title": {"required": False, "allow_blank": True},
            "user_name": {"required": False, "allow_blank": True},
            "user_email": {"required": False, "allow_blank": True},
        }

    def validate_rating(self, value):
        v = int(value)
        if v < 1 or v > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return v

    def to_internal_value(self, data):
        # Accept either `comment` or `body`
        if "comment" in data and "body" not in data:
            data = {**data, "body": data.get("comment")}
        return super().to_internal_value(data)

    def create(self, validated):
        req = self.context.get("request")
        # attach user if authenticated
        if req and getattr(req, "user", None) and req.user.is_authenticated:
            validated["user"] = req.user
            validated.setdefault("user_name", req.user.get_full_name() or req.user.email.split("@")[0])
            validated.setdefault("user_email", req.user.email)
        # client info
        if req:
            validated["ip_address"] = req.META.get("REMOTE_ADDR")
            validated["user_agent"] = req.META.get("HTTP_USER_AGENT", "")
        return super().create(validated)


class PromoBannerSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromoBanner
        fields = "__all__"


class ProductGridSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductGrid
        fields = "__all__"


class SpecialOfferSerializer(serializers.ModelSerializer):
    class Meta:
        model = SpecialOffer
        fields = "__all__"


class ProductCollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCollection
        fields = "__all__"


# ---------- Inline variant write serializer ----------

class WriteVariantInlineSerializer(serializers.Serializer):
    sku = serializers.CharField(required=False, allow_blank=True)
    weight_value = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    weight_unit  = serializers.ChoiceField(choices=[c[0] for c in UNIT_CHOICES], required=False, allow_null=True)
    price        = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    stock        = serializers.IntegerField(required=False, min_value=0)
    is_active    = serializers.BooleanField(required=False, default=True)
    mrp          = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    min_order_qty = serializers.IntegerField(required=False, min_value=1, default=1)
    step_qty      = serializers.IntegerField(required=False, min_value=1, default=1)
    attributes    = serializers.DictField(child=serializers.CharField(), required=False)

    def validate(self, attrs):
        attrs["weight_value"] = _to_decimal(attrs.get("weight_value"), allow_none=True)
        attrs["price"]        = _to_decimal(attrs.get("price"), allow_none=True)
        attrs["mrp"]          = _to_decimal(attrs.get("mrp"), allow_none=True)
        return attrs


# ---------- Product (Create/Update – V2 with inline variants & images_meta) ----------
# IMPORTANT: Ensure there is only ONE class with this name in the file.
class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    category_id = IDRelatedField(queryset=Category.objects.all(), source="category", required=True, allow_null=False)
    vendor_id   = IDRelatedField(queryset=Vendor.objects.all(),   source="vendor",   required=False, allow_null=True)
    store_id    = IDRelatedField(queryset=Store.objects.all(),    source="store",    required=False, allow_null=True)

    price_inr   = serializers.CharField(required=False, allow_blank=True, default="0.00")
    gst_rate    = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="0.00")
    mrp_price   = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    cost_price  = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    default_pack_qty = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    variants = WriteVariantInlineSerializer(many=True, required=False)
    images_meta = serializers.ListField(child=serializers.DictField(), required=False)

    class Meta:
        model = Product
        fields = [
            "id",  # ✅ include id so create/update responses return the product id
            "category_id", "vendor_id", "store_id",
            "name", "description", "origin_country", "grade",
            "quantity", "manufacture_date", "is_perishable", "is_organic", "shelf_life_days",
            "default_uom", "default_pack_qty",
            "price_inr", "discount_percent", "hsn_sac", "gst_rate", "mrp_price", "cost_price",
            "featured", "new_arrival", "hot_deal", "hot_deal_ends_at",
            "warranty_months", "is_published",
            "ingredients", "allergens", "nutrition_facts", "nutrition_notes",
            "variants", "images_meta",
        ]
        read_only_fields = ["id"]

    def to_internal_value(self, data):
        def _maybe_json(value, default):
            if value is None:
                return default
            if isinstance(value, (list, dict)):
                return value
            if isinstance(value, str):
                s = value.strip()
                if not s:
                    return default
                try:
                    return json.loads(s)
                except Exception:
                    return default
            return default

        if hasattr(data, "mutable") and not data.mutable:
            data = data.copy()

        data["variants"] = _maybe_json(data.get("variants"), [])
        data["images_meta"] = _maybe_json(data.get("images_meta"), [])
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs["price_inr"]  = _to_decimal(attrs.get("price_inr"), default="0.00")
        attrs["gst_rate"]   = _to_decimal(attrs.get("gst_rate"), allow_none=True)
        attrs["mrp_price"]  = _to_decimal(attrs.get("mrp_price"), allow_none=True)
        attrs["cost_price"] = _to_decimal(attrs.get("cost_price"), allow_none=True)
        dpk = attrs.get("default_pack_qty")
        attrs["default_pack_qty"] = _to_decimal(dpk, allow_none=True)
        return attrs

    def _create_or_update_variants(self, product, items):
        out = []
        for v in items or []:
            sku = (v.get("sku") or "").strip()
            if not sku:
               w = (v.get("weight_value") or "").strip()
               u = (v.get("weight_unit") or "").strip().upper()
               if not (w and u):
                  raise serializers.ValidationError("Each variant needs either sku or (weight_value & weight_unit).")
               sku = f"{product.slug}-{w}{u.lower()}"

            def _dec(x, default=None):
                if x in (None, "", "null"):
                    return default
                try:
                    return Decimal(str(x))
                except Exception:
                    return default

            defaults = dict(
                attributes    = v.get("attributes") or {"Weight": f'{v.get("weight_value")}{(v.get("weight_unit") or "").upper()}'},
                weight_value  = _dec(v.get("weight_value")),
                weight_unit   = (v.get("weight_unit") or "").upper() or None,
                price_override= _dec(v.get("price"), None),
                quantity      = int(v.get("stock") or 0),
                is_active     = bool(v.get("is_active", True)),
                mrp           = _dec(v.get("mrp"), None),
                min_order_qty = int(v.get("min_order_qty") or 1),
                step_qty      = int(v.get("step_qty") or 1),
            )
            obj, _ = ProductVariant.objects.update_or_create(product=product, sku=sku, defaults=defaults)
            out.append(obj)
        return out

    def _handle_images_from_request(self, product: Product):
        request = self.context.get("request")
        if not request:
            return

        files = request.FILES.getlist("images")
        if not files:
            return

        meta_list = self.validated_data.get("images_meta") or []
        meta = { (m.get("filename") or "").strip(): bool(m.get("is_primary")) for m in meta_list }

        created = []
        for f in files:
            is_primary = meta.get(f.name, False)
            img = ProductImage.objects.create(product=product, image=f, is_primary=is_primary)
            created.append(img)

        primary_obj = next((im for im in created if im.is_primary), None)
        if not primary_obj:
            existing_primary = ProductImage.objects.filter(product=product, is_primary=True).exclude(pk__in=[im.pk for im in created]).first()
            primary_obj = existing_primary or (created[0] if created else None)

        ProductImage.objects.filter(product=product).update(is_primary=False)
        if primary_obj:
            primary_obj.is_primary = True
            primary_obj.save(update_fields=["is_primary"])

    @transaction.atomic
    def create(self, validated_data):
        variants = validated_data.pop("variants", None)
        validated_data.pop("images_meta", None)

        product = Product.objects.create(**validated_data)

        if variants:
            self._create_or_update_variants(product, variants)

        self._handle_images_from_request(product)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        variants = validated_data.pop("variants", None)
        validated_data.pop("images_meta", None)

        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()

        if variants is not None:
            self._create_or_update_variants(instance, variants)

        self._handle_images_from_request(instance)
        return instance

# ---------- Cart / Order basic serializers for admin ----------

class CartItemThinSerializer(serializers.ModelSerializer):
    class Meta:
        model = CartItem
        fields = ["id", "product", "variant", "quantity", "created_at", "updated_at"]


class CartThinSerializer(serializers.ModelSerializer):
    items = CartItemThinSerializer(many=True, read_only=True)

    class Meta:
        model = Cart
        fields = ["id", "user", "checked_out", "items", "created_at", "updated_at"]


class OrderCheckoutDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderCheckoutDetails
        fields = [
            "full_name", "email", "phone",
            "address1", "address2", "city", "state",
            "postcode", "country", "notes",
        ]

class OrderPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderPayment
        fields = [
            "method", "provider", "status", "transaction_id",
            "currency", "amount", "raw", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

class OrderLineSerializer(serializers.Serializer):
    """Read-only ‘line’ built from the cart items at the time you return the order."""
    product_id   = serializers.IntegerField()
    variant_id   = serializers.IntegerField(allow_null=True)
    name         = serializers.CharField()
    qty          = serializers.IntegerField()
    price        = serializers.DecimalField(max_digits=12, decimal_places=2)
    image        = serializers.CharField(allow_blank=True, required=False)
    weight       = serializers.CharField(allow_blank=True, required=False)

class ProductMiniSerializer(serializers.ModelSerializer):
    primary_image_url = serializers.SerializerMethodField()

    class Meta:
        model  = Product
        fields = ("id", "slug", "name", "primary_image_url")

    def get_primary_image_url(self, obj: Product) -> str:
        url = obj.primary_image_url or ""
        req = self.context.get("request")
        if url and req and not url.startswith("http"):
            return req.build_absolute_uri(url)
        return url


class VariantMiniSerializer(serializers.ModelSerializer):
    primary_image_url = serializers.SerializerMethodField()

    class Meta:
        model  = ProductVariant
        fields = ("id", "sku", "attributes", "primary_image_url")

    def get_primary_image_url(self, obj: ProductVariant) -> str:
        # primary variant image → first variant image → product.primary_image_url
        img = obj.images.filter(is_primary=True).first() or obj.images.first()
        url = img.image.url if (img and img.image) else (obj.product.primary_image_url or "")
        req = self.context.get("request")
        if url and req and not url.startswith("http"):
            return req.build_absolute_uri(url)
        return url


class CartItemSerializer(serializers.ModelSerializer):
    product = ProductMiniSerializer(read_only=True)
    variant = VariantMiniSerializer(read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model  = CartItem
        fields = ("id", "product", "variant", "quantity", "unit_price", "line_total")


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)

    class Meta:
        model  = Cart
        fields = ("id", "checked_out", "items")


class OrderSerializer(serializers.ModelSerializer):
    checkout_details = serializers.SerializerMethodField()
    payment          = OrderPaymentSerializer(read_only=True)
    lines            = serializers.SerializerMethodField()
    totals           = serializers.SerializerMethodField()
    cart             = CartSerializer(read_only=True)

    class Meta:
        model  = Order
        fields = [
            "id", "status", "payment_method",
            "country_code", "currency",
            "created_at", "updated_at",
            "checkout_details", "payment",
            "lines", "totals", "cart",
        ]

    def _country(self) -> str:
        return (self.context.get("country_code") or "IN").upper()

    def get_checkout_details(self, obj: Order):
        det = getattr(obj, "checkout_details", None)
        return OrderCheckoutDetailsSerializer(det).data if det else None

    def get_lines(self, obj: Order):
        """
        Build line snapshots from the cart safely (cart may be missing).
        """
        cart = getattr(obj, "cart", None)
        if cart is None:
            return []

        out = []
        req = self.context.get("request")
        cc  = self._country()

        # Use the already-prefetched relations when available
        items_qs = cart.items.select_related("product", "variant", "product__category").all()
        for it in items_qs:
            # unit price
            if it.variant_id:
                price = it.variant.unit_price_for_country(cc)
            else:
                price = it.product.discounted_price_for_country(cc)

            # image url
            img = ""
            vimg = it.variant and it.variant.images.filter(is_primary=True).first()
            if vimg and vimg.image:
                img = _absolute_media_url(req, vimg.image)
            else:
                prim = it.product.primary_image()
                if prim:
                    img = _absolute_media_url(req, prim.image)

            out.append(dict(
                product_id=it.product_id,
                variant_id=it.variant_id,
                name=str(it),
                qty=int(it.quantity),
                price=Decimal(price),
                image=img or "",
                weight=(str(it.variant.weight_value) + (it.variant.weight_unit or "")) if it.variant_id else "",
            ))
        return OrderLineSerializer(out, many=True).data

    def get_totals(self, obj: Order):
        """
        Compute totals from cart items (shipping/tax 0 for now).
        Guard for orders without a cart.
        """
        cart = getattr(obj, "cart", None)
        if cart is None:
            return {
                "subtotal": "0.00",
                "shipping": "0.00",
                "tax":      "0.00",
                "grand_total": "0.00",
            }

        cc = self._country()
        subtotal = Decimal("0.00")
        for it in cart.items.select_related("product", "variant").all():
            if it.variant_id:
                unit = it.variant.unit_price_for_country(cc)
            else:
                unit = it.product.discounted_price_for_country(cc)
            subtotal += (Decimal(unit) * it.quantity)

        shipping = Decimal("0.00")
        tax      = Decimal("0.00")
        total    = (subtotal + shipping + tax).quantize(Decimal("0.01"))
        return {
            "subtotal": f"{subtotal:.2f}",
            "shipping": f"{shipping:.2f}",
            "tax":      f"{tax:.2f}",
            "grand_total": f"{total:.2f}",
        }

# uses _absolute_media_url(request, file_or_path) already defined in this file

class TestimonialSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model  = Testimonial
        fields = [
            "id", "name", "location", "rating", "testimonial",
            "product", "avatar", "avatar_url", "verified",
            "is_active", "sort", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        return _absolute_media_url(request, getattr(obj, "avatar", None))


class VideoTestimonialSerializer(serializers.ModelSerializer):
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model  = VideoTestimonial
        fields = [
            "id", "name", "description", "thumbnail", "thumbnail_url",
            "video_file", "video_url", "duration",
            "is_active", "sort", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_thumbnail_url(self, obj):
        request = self.context.get("request")
        return _absolute_media_url(request, getattr(obj, "thumbnail", None))


class AwardRecognitionSerializer(serializers.ModelSerializer):
    emblem_url = serializers.SerializerMethodField()

    class Meta:
        model  = AwardRecognition
        fields = [
            "id", "title", "organization", "year", "description",
            "category", "emblem", "emblem_url",
            "is_active", "sort", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_emblem_url(self, obj):
        request = self.context.get("request")
        return _absolute_media_url(request, getattr(obj, "emblem", None))


class CertificationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Certification
        fields = [
            "id", "name", "authority", "valid_until", "description",
            "is_active", "sort", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class GalleryItemSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model  = GalleryItem
        fields = [
            "id", "category", "image", "image_url", "title",
            "location", "date_label", "description", "attendees",
            "is_active", "sort", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_image_url(self, obj):
        request = self.context.get("request")
        return _absolute_media_url(request, getattr(obj, "image", None))



class BlogCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogCategory
        fields = ["id", "name", "slug", "description", "image", "created_at"]


class BlogPostVersionSerializer(serializers.ModelSerializer):
    editor_name = serializers.SerializerMethodField()

    class Meta:
        model = BlogPostVersion
        fields = [
            "version", "title", "excerpt", "content_markdown", "content_html",
            "tags_csv", "editor", "editor_name", "created_at"
        ]

    def get_editor_name(self, obj):
        return (obj.editor.get_full_name() or obj.editor.email) if obj.editor else None


class BlogPostSerializer(serializers.ModelSerializer):
    category = BlogCategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(source="category", queryset=BlogCategory.objects.all(), write_only=True)
    author_name = serializers.SerializerMethodField()
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    cover_url = serializers.SerializerMethodField()
    versions = BlogPostVersionSerializer(many=True, read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            "id", "title", "slug", "excerpt",
            "content_markdown", "content_html",
            "cover", "cover_url",
            "category", "category_id",
            "author", "author_name",
            "tags", "tags_csv",
            "featured", "is_published", "published_at",
            "views_count",
            "created_at", "updated_at",
            "versions",
        ]
        read_only_fields = ["author", "author_name", "slug", "created_at", "updated_at", "views_count", "tags_csv"]

    def get_author_name(self, obj):
        u = obj.author
        return (u.get_full_name() or u.email.split("@")[0]) if u else None

    def get_cover_url(self, obj):
        request = self.context.get("request")
        if obj.cover and hasattr(obj.cover, "url"):
            if request:
                return request.build_absolute_uri(obj.cover.url)
            return obj.cover.url
        return None

    def to_internal_value(self, data):
        # Accept either tags[] or comma-separated tags_csv
        ret = super().to_internal_value(data)
        tags_list = data.get("tags")
        if tags_list is None:
            tags_csv = data.get("tags_csv") or ""
            tags_list = [t.strip() for t in str(tags_csv).split(",") if t.strip()]
        ret["tags"] = tags_list
        return ret

    def _snapshot_version(self, post: BlogPost, editor):
        last = post.versions.order_by("-version").first()
        next_ver = 1 + (last.version if last else 0)
        BlogPostVersion.objects.create(
            post=post,
            version=next_ver,
            title=post.title,
            excerpt=post.excerpt,
            content_markdown=post.content_markdown,
            content_html=post.content_html,
            tags_csv=post.tags_csv,
            editor=editor,
        )

    def create(self, validated):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        tags = validated.pop("tags", [])
        post = BlogPost.objects.create(author=user if user and user.is_authenticated else None, **validated)
        post.set_tags(tags or [])
        post.save()
        self._snapshot_version(post, user)
        return post

    def update(self, instance, validated):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        tags = validated.pop("tags", None)
        for k, v in validated.items():
            setattr(instance, k, v)
        if tags is not None:
            instance.set_tags(tags)
        instance.save()
        self._snapshot_version(instance, user)
        return instance

# --- Carts ---




