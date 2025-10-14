from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views  # keep module import so router can see all viewsets
from .views import RazorpayCreateOrder, RazorpayVerifyPayment

router = DefaultRouter()
router.register(r"categories", views.CategoryViewSet, basename="category")
router.register(r"products", views.ProductViewSet, basename="product")
router.register(r"product-images", views.ProductImageViewSet, basename="productimage")
router.register(r"variants", views.ProductVariantViewSet, basename="variant")
router.register(r"variant-images", views.VariantImageViewSet, basename="variantimage")
router.register(r"carts", views.CartViewSet, basename="cart")
router.register(r"orders", views.OrderViewSet, basename="order")

# 🔧 remove stray spaces after `views.`
router.register(r"stores", views.StoreViewSet, basename="store")
router.register(r"vendors", views.VendorViewSet, basename="vendor")

router.register(r"contacts", views.ContactSubmissionViewSet, basename="contacts")
router.register(r"reviews", views.ProductReviewViewSet, basename="review")
router.register(r"visits", views.VisitEventViewSet, basename="visitevent")
router.register(r"wishlist", views.WishlistViewSet, basename="wishlist")
router.register(r"wishlist-items", views.WishlistItemViewSet, basename="wishlistitem")

router.register(r"special-offers", views.SpecialOfferViewSet, basename="specialoffer")
router.register(r"product-grids", views.ProductGridViewSet, basename="productgrid")
router.register(r"promo-banners", views.PromoBannerViewSet, basename="promobanner")
router.register(r"product-collections", views.ProductCollectionViewSet, basename="productcollection")

router.register(r"blog/categories", views.BlogCategoryViewSet, basename="blog-category")
router.register(r"blog/posts", views.BlogPostViewSet, basename="blog-post")

router.register(r"jobs/openings", views.JobOpeningViewSet, basename="job-opening")
router.register(r"jobs/applications", views.JobApplicationViewSet, basename="job-application")

router.register(r"payments/razorpay", views.RazorpayPaymentViewSet, basename="payments-razorpay")
router.register(r"testimonials", views.TestimonialViewSet, basename="testimonial")
router.register(r"video-testimonials", views.VideoTestimonialViewSet, basename="video-testimonial")

router.register(r"awards", views.AwardRecognitionViewSet, basename="award")
router.register(r"certifications", views.CertificationViewSet, basename="certification")
router.register(r"gallery", views.GalleryItemViewSet, basename="gallery-item")

urlpatterns = [
    path("api/", include(router.urls)),

    # 🔐 custom email-based auth endpoints
    path("api/auth/token/", views.EmailObtainAuthToken.as_view(), name="auth-token"),
    path("api/auth/register/", views.RegisterView.as_view(), name="auth-register"),
    path("api/auth/me/", views.MeView.as_view(), name="auth-me"),  # ← NEW
    # 📊 analytics
    path("api/analytics/sales-series/", views.SalesSeriesView.as_view(), name="sales-series"),
    path("api/analytics/kpis/", views.DashboardKpiView.as_view(), name="analytics-kpis"),
    path("api/dashboard/kpis/", views.DashboardKpiView.as_view(), name="dashboard-kpis"),

    # 💳 Razorpay helpers
    path("api/payments/razorpay/create-order/", RazorpayCreateOrder.as_view()),
    path("api/payments/razorpay/verify/", RazorpayVerifyPayment.as_view()),
]
