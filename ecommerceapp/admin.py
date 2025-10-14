from decimal import Decimal
from django.contrib import admin
from django.db import models
from django.utils.html import format_html
from django.forms import Textarea

from .models import *

READONLY_TS = ("created_at", "updated_at")

@admin.register(User)
class userAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "date_joined")
    search_fields = ("email", "first_name", "last_name")
    list_filter = ("is_active", "is_staff", "is_superuser")
    readonly_fields = ("date_joined", "last_login")
    fields = ("email", "first_name", "last_name", "is_active", "is_staff", "is_superuser", "date_joined", "last_login")
# ───────── Inlines ─────────
class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0
    fields = ("image", "preview", "is_primary",) + READONLY_TS
    readonly_fields = READONLY_TS + ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if obj and getattr(obj, "image", None):
            return format_html('<img src="{}" style="height:60px;width:auto;border-radius:4px;" />', obj.image.url)
        return "—"

class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ("sku", "quantity", "is_active", "price_override", "discount_override",
              "weight_value","weight_unit","mrp", "barcode", "min_order_qty", "step_qty") + READONLY_TS
    readonly_fields = READONLY_TS

class ProductSpecificationInline(admin.TabularInline):
    model = ProductSpecification
    extra = 0
    fields = ("group", "name", "value", "unit", "is_highlight", "sort_order") + READONLY_TS
    readonly_fields = READONLY_TS

# ───────── Store / Vendor / Color ─────────
@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name","city","is_active","created_at")
    search_fields = ("name","city")
    readonly_fields = READONLY_TS
    fields = ("name","slug","logo","email","phone","address1","address2","city","state","postcode","country","is_active") + READONLY_TS

@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("display_name","user","store","is_active","total_units_sold","total_revenue","created_at")
    list_filter = ("is_active","store")
    search_fields = ("display_name","user__email")
    readonly_fields = READONLY_TS
    fields = ("user","display_name","store","is_active","total_units_sold","total_revenue") + READONLY_TS

@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("name","hex")
    search_fields = ("name","hex")

# ───────── Category ─────────
@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "parent", "created_at", "updated_at")
    list_filter = ("parent",)
    search_fields = ("name", "slug", "parent__name")
    readonly_fields = READONLY_TS
    fields = ("name", "slug", "parent", "icon", "image") + READONLY_TS

# ───────── Product & Images / Variants ─────────
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "thumb",  # thumbnail column
        "id", "name", "category",
        "price_inr", "discount_percent",
        "quantity", "in_stock", "limited_stock",
        "featured", "new_arrival", "is_published",
        "created_at",
    )
    list_filter = (
        "category", "in_stock", "limited_stock",
        "featured", "new_arrival", "hot_deal",
        "vendor","store","is_perishable","is_organic",
        "is_published",
    )
    search_fields = ("name", "slug", "category__name", "description", "ingredients", "allergens")
    readonly_fields = READONLY_TS + (
        "views_count", "carts_count", "sold_count", "reviews_count",
        "rating_avg", "wishes_count",
    )
    inlines = [ProductImageInline, ProductVariantInline, ProductSpecificationInline]

    # Make TextField bigger and note HTML support in description
    formfield_overrides = {
        models.TextField: {
            "widget": Textarea(attrs={"rows": 10, "style": "font-family:monospace"}),
        }
    }

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "description" in form.base_fields:
            form.base_fields["description"].help_text = (
                "You can paste HTML here (e.g., &lt;h1&gt;, &lt;h2&gt;, &lt;p&gt;). "
                "It will render exactly on the product page."
            )
        return form

    fields = (
        # Core
        "category", "name", "slug", "description",
        "vendor","store",
        # Inventory / attributes
        "quantity", "grade", "manufacture_date", "origin_country", "warranty_months",
        # Pricing
        "price", "discount_percent",
        "price_inr", "price_usd",
        "aed_pricing_mode", "price_aed_static",
        "gold_weight_g", "gold_making_charge", "gold_markup_percent",
        # Grocery specifics
        "default_uom","default_pack_qty","is_organic","is_perishable","shelf_life_days",
        "hsn_sac","gst_rate","mrp_price","cost_price",
        # Nutrition
        "ingredients", "allergens", "nutrition_facts", "nutrition_notes",
        # Flags / visibility
        "in_stock", "limited_stock", "featured", "new_arrival",
        "hot_deal", "hot_deal_ends_at",
        "is_published",
        # Stats (RO)
        "views_count", "carts_count", "sold_count", "reviews_count", "rating_avg", "wishes_count",
    ) + READONLY_TS

    @admin.display(description="", ordering=None)
    def thumb(self, obj):
        url = getattr(obj, "primary_image_url", "") or ""
        if not url:
            return "—"
        return format_html('<img src="{}" style="height:40px;width:auto;border-radius:4px" />', url)

@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "is_primary", "created_at")
    list_filter = ("is_primary",)
    search_fields = ("product__name",)
    readonly_fields = READONLY_TS
    fields = ("product", "image", "is_primary") + READONLY_TS

@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "sku", "quantity", "is_active",
                    "weight_value","weight_unit","color","mrp",
                    "price_override", "discount_override", "created_at")
    list_filter = ("is_active", "product","weight_unit","color")
    search_fields = ("sku", "product__name")
    readonly_fields = READONLY_TS
    fields = ("product", "sku", "attributes",
              "price_override", "discount_override",
              "quantity", "is_active",
              "weight_value","weight_unit","color","mrp","barcode",
              "min_order_qty","step_qty") + READONLY_TS

@admin.register(VariantImage)
class VariantImageAdmin(admin.ModelAdmin):
    list_display = ("id", "variant", "is_primary", "created_at")
    list_filter = ("is_primary",)
    search_fields = ("variant__sku", "variant__product__name")
    readonly_fields = READONLY_TS
    fields = ("variant", "image", "is_primary") + READONLY_TS

# ───────── Options ─────────
@admin.register(ProductOption)
class ProductOptionAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "name", "values_count")
    search_fields = ("product__name", "name")
    list_select_related = ("product",)

    @admin.display(description="Values")
    def values_count(self, obj):
        return obj.values.count()

@admin.register(ProductOptionValue)
class ProductOptionValueAdmin(admin.ModelAdmin):
    list_display = ("id", "option", "value")
    search_fields = ("option__name", "value", "option__product__name")
    list_select_related = ("option", "option__product")

# ───────── Cart / Order ─────────
@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "checked_out", "items_count", "created_at", "updated_at")
    search_fields = ("user__email",)
    list_filter = ("checked_out",)
    readonly_fields = READONLY_TS
    fields = ("user", "checked_out") + READONLY_TS

    @admin.display(description="Items")
    def items_count(self, obj):
        return obj.items.count()

@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("id", "cart", "product", "variant", "quantity", "unit_price_display", "line_total_display", "created_at")
    search_fields = ("cart__user__email", "product__name", "variant__sku")
    list_select_related = ("cart", "product", "variant")
    readonly_fields = READONLY_TS
    fields = ("cart", "product", "variant", "quantity") + READONLY_TS

    @admin.display(description="Unit price")
    def unit_price_display(self, obj):
        try:
            return f"{obj.unit_price:.2f}"
        except Exception:
            return "-"

    @admin.display(description="Line total")
    def line_total_display(self, obj):
        try:
            return f"{obj.line_total:.2f}"
        except Exception:
            return "-"

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "total_amount", "items_count", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email", "id")
    readonly_fields = READONLY_TS
    fields = ("user", "cart", "status") + READONLY_TS

    @admin.display(description="Items")
    def items_count(self, obj):
        return obj.cart.items.count()

    @admin.display(description="Total")
    def total_amount(self, obj):
        total = Decimal("0.00")
        for it in obj.cart.items.select_related("product", "variant"):
            try:
                total += it.line_total
            except Exception:
                unit = it.variant.unit_price if it.variant_id else it.product.discounted_price
                total += (unit * it.quantity)
        return f"{total:.2f}"

@admin.register(OrderCheckoutDetails)
class OrderCheckoutDetailsAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "full_name", "email", "phone", "city", "country", "created_at")
    search_fields = ("order__id", "full_name", "email", "phone", "city", "country")
    list_select_related = ("order",)
    readonly_fields = READONLY_TS
    fields = ("order", "full_name", "email", "phone", "address1", "address2", "city", "state", "postcode", "country", "notes") + READONLY_TS

# ───────── Contact / Reviews / Visits ─────────
@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "phone", "handled", "created_at")
    list_filter = ("handled",)
    search_fields = ("name", "email", "phone", "subject")
    readonly_fields = READONLY_TS
    fields = ("user", "name", "email", "phone", "subject", "message", "ip_address", "user_agent", "page_url", "handled") + READONLY_TS

@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "user", "rating", "is_approved", "created_at")
    list_filter = ("is_approved", "rating")
    search_fields = ("product__name", "user__email", "title", "body")
    list_select_related = ("product", "user")
    readonly_fields = READONLY_TS
    fields = ("product", "user", "rating", "title", "body", "is_approved", "ip_address", "user_agent") + READONLY_TS

@admin.register(VisitEvent)
class VisitEventAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "user", "method", "path", "ip_address")
    list_filter = ("method",)
    search_fields = ("user__email", "path", "referer", "ip_address", "user_agent")
    list_select_related = ("user",)
    readonly_fields = READONLY_TS
    fields = ("user", "ip_address", "user_agent", "method", "path", "referer") + READONLY_TS

# ───────── Wishlist ─────────
@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at")
    search_fields = ("user__email",)
    list_select_related = ("user",)
    readonly_fields = READONLY_TS
    fields = ("user",) + READONLY_TS

@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    list_display = ("id", "wishlist", "product", "variant", "created_at")
    search_fields = ("wishlist__user__email", "product__name", "variant__sku")
    list_select_related = ("wishlist", "product", "variant")
    readonly_fields = READONLY_TS
    fields = ("wishlist", "product", "variant") + READONLY_TS

# ───────── Marketing ─────────
@admin.register(SpecialOffer)
class SpecialOfferAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "percentage", "is_active", "starts_at", "ends_at", "sort_order", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "subtitle", "description", "badge", "cta_label", "cta_url")
    readonly_fields = READONLY_TS
    fields = (
        "title", "subtitle", "percentage", "description",
        "image", "badge", "cta_label", "cta_url", "query_params",
        "starts_at", "ends_at", "is_active", "sort_order",
    ) + READONLY_TS

@admin.register(ProductCollection)
class ProductCollectionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "is_active", "default_limit", "default_order", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    filter_horizontal = ("products",)
    readonly_fields = READONLY_TS
    fields = ("name", "slug", "description", "query", "products", "default_limit", "default_order", "is_active") + READONLY_TS

@admin.register(ProductGrid)
class ProductGridAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "product", "is_active", "sort", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "subtitle", "badge", "discount_text", "product__name")
    list_select_related = ("product",)
    readonly_fields = READONLY_TS
    fields = (
        "title", "subtitle",
        "price_text", "original_price_text",
        "badge", "discount_text",
        "image", "image_url",
        "product",
        "sort", "is_active",
    ) + READONLY_TS

@admin.register(PromoBanner)
class PromoBannerAdmin(admin.ModelAdmin):
    list_display = ("id", "placement", "variant", "title", "is_active", "sort", "starts_at", "ends_at", "created_at")
    list_filter = ("placement", "variant", "is_active")
    search_fields = ("title", "subtitle", "badge", "button_text", "cta_url", "coupon_code", "offer_text", "main_offer")
    readonly_fields = READONLY_TS
    fields = (
        "placement", "variant",
        "title", "subtitle", "badge",
        "button_text", "cta_url",
        "image", "image_url",
        "class_name", "overlay_class", "is_wide",
        "coupon_code", "coupon_text",
        "offer_text", "main_offer",
        "is_active", "starts_at", "ends_at",
        "sort",
    ) + READONLY_TS

@admin.register(BlogCategory)
class BlogCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

class BlogPostVersionInline(admin.TabularInline):
    model = BlogPostVersion
    extra = 0
    readonly_fields = ("version", "editor", "created_at", "title")

@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "is_published", "featured", "published_at", "views_count")
    list_filter = ("is_published", "featured", "category")
    search_fields = ("title", "excerpt", "content_markdown", "tags_csv")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [BlogPostVersionInline]

@admin.register(JobOpening)
class JobOpeningAdmin(admin.ModelAdmin):
    list_display = ["title", "department", "location", "employment_type", "is_active", "posted_at"]
    list_filter = ["is_active", "employment_type", "department"]
    search_fields = ["title", "description", "location"]

@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ["full_name", "email", "job", "status", "created_at"]
    list_filter = ["status", "job"]
    search_fields = ["full_name", "email", "phone", "cover_letter"]
    autocomplete_fields = ["job"]



@admin.register(Testimonial)
class TestimonialAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "rating", "verified", "is_active", "sort", "created_at")
    list_filter  = ("verified", "is_active", "rating")
    search_fields = ("name", "location", "testimonial", "product")
    ordering = ("sort", "-created_at")


@admin.register(VideoTestimonial)
class VideoTestimonialAdmin(admin.ModelAdmin):
    list_display = ("name", "duration", "is_active", "sort", "created_at")
    list_filter  = ("is_active",)
    search_fields = ("name", "description")
    ordering = ("sort", "-created_at")


@admin.register(AwardRecognition)
class AwardRecognitionAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "year", "category", "is_active", "sort", "created_at")
    list_filter  = ("category", "is_active")
    search_fields = ("title", "organization", "description", "year")
    ordering = ("sort", "-created_at")


@admin.register(Certification)
class CertificationAdmin(admin.ModelAdmin):
    list_display = ("name", "authority", "valid_until", "is_active", "sort", "created_at")
    list_filter  = ("is_active",)
    search_fields = ("name", "authority", "description", "valid_until")
    ordering = ("sort", "-created_at")


@admin.register(GalleryItem)
class GalleryItemAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "location", "date_label", "attendees", "is_active", "sort", "created_at")
    list_filter  = ("category", "is_active")
    search_fields = ("title", "location", "description", "attendees", "date_label")
    ordering = ("category", "sort", "-created_at")
