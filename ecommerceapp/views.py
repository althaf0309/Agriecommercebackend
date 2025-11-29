from decimal import Decimal
from django.db.models import F, Sum, Count
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from django.utils import timezone
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.conf import settings
import hmac
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
import hashlib
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email as django_validate_email
import razorpay
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ValidationError
from .models import *
from .serializers import *
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from django.core.mail import EmailMultiAlternatives
from django.template.defaultfilters import linebreaksbr, floatformat
from django.utils.html import escape
from django.contrib.auth import get_user_model
User = get_user_model()
# ---------- country helper ----------

def _country_code(request):
    return (request.headers.get("X-Country-Code") or request.query_params.get("country") or "IN").upper()

# ---------- permissions ----------
def _from_email():
    return getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None) or "no-reply@example.com"

def _split_csv_emails(value: str | None):
    if not value:
        return []
    return [e.strip() for e in str(value).replace(";", ",").split(",") if e.strip()]

def _admin_recipients():
    # try BACKEND_NOTIFY_EMAILS first; fallback to CONTACT_NOTIFY_EMAILS / LEAD_NOTIFY_EMAILS
    for key in ("BACKEND_NOTIFY_EMAILS", "CONTACT_NOTIFY_EMAILS", "LEAD_NOTIFY_EMAILS"):
        emails = _split_csv_emails(getattr(settings, key, None))
        if emails:
            return emails
    # last resort: project email itself
    fallback = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)
    return [fallback] if fallback else []

def _abs(req, path_or_url: str | None):
    """Best-effort absolute URL for images/links in emails."""
    if not path_or_url:
        return ""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    try:
        return req.build_absolute_uri(path_or_url)
    except Exception:
        return path_or_url

def _currency_prefix(order):
    return "₹" if (getattr(order, "currency", "INR") or "INR").upper() == "INR" else ""

def _format_money(order, value):
    prefix = _currency_prefix(order)
    try:
        amt = Decimal(str(value or "0"))
    except Exception:
        amt = Decimal("0.00")
    return f"{prefix}{amt.quantize(Decimal('0.01'))}"

def _collect_line_items_for_email(order, request):
    """Return list of dicts with name, qty, unit_price, line_total, image, weight."""
    cc = (getattr(order, "country_code", "IN") or "IN").upper()
    out = []
    if not getattr(order, "cart", None):
        return out
    items = order.cart.items.select_related("product", "variant", "product__category").all()
    for it in items:
        if it.variant_id:
            unit = it.variant.unit_price_for_country(cc)
            weight = f"{it.variant.weight_value or ''}{(it.variant.weight_unit or '')}"
        else:
            unit = it.product.discounted_price_for_country(cc)
            weight = ""
        try:
            unit_dec = Decimal(str(unit or "0"))
        except Exception:
            unit_dec = Decimal("0.00")
        line_total = (unit_dec * it.quantity).quantize(Decimal("0.01"))

        # image
        img = ""
        vimg = it.variant and it.variant.images.filter(is_primary=True).first()
        if vimg and vimg.image:
            img = _abs(request, vimg.image.url if hasattr(vimg.image, "url") else str(vimg.image))
        else:
            pimg = it.product.primary_image()
            if pimg and pimg.image:
                img = _abs(request, pimg.image.url if hasattr(pimg.image, "url") else str(pimg.image))

        out.append({
            "name": str(it),
            "qty": int(it.quantity),
            "unit": unit_dec,
            "line_total": line_total,
            "image": img,
            "weight": weight,
        })
    return out

def _render_order_email_parts(order, request, heading_for_customer=True):
    """Return (subject, text_body, html_body)."""
    prefix = _currency_prefix(order)
    lines = _collect_line_items_for_email(order, request)

    subtotal = Decimal("0.00")
    for l in lines:
        subtotal += l["line_total"]
    shipping = Decimal("0.00")
    tax = Decimal("0.00")
    total = (subtotal + shipping + tax).quantize(Decimal("0.01"))

    # customer details
    det = getattr(order, "checkout_details", None)
    full_name = ""
    email = ""
    phone = ""
    address = ""
    if det:
        full_name = getattr(det, "full_name", "") or ""
        email = getattr(det, "email", "") or ""
        phone = getattr(det, "phone", "") or ""
        parts = [
            getattr(det, "address1", "") or "",
            getattr(det, "address2", "") or "",
            ", ".join(
                [
                    x
                    for x in [
                        getattr(det, "city", ""),
                        getattr(det, "state", ""),
                        getattr(det, "postcode", ""),
                    ]
                    if x
                ]
            ),
            getattr(det, "country", "") or "",
        ]
        address = "\n".join([p for p in parts if p])

    subject = f"Order #{order.id} {'Placed' if heading_for_customer else 'Notification'}"

    # Plain text
    lines_text_list = []
    for l in lines:
        weight_str = f" ({l.get('weight')})" if l.get("weight") else ""
        lines_text_list.append(
            f"- {l['name']}{weight_str} x {int(l['qty'])} @ {prefix}{l['unit']:.2f} = {prefix}{l['line_total']:.2f}"
        )
    lines_text = "\n".join(lines_text_list) or "(no items)"

    text = (
        f"{'Thank you for your purchase!' if heading_for_customer else 'A new order has been placed.'}\n\n"
        f"Order ID: #{order.id}\n"
        f"Status: {order.status}\n"
        f"Date: {timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M')}\n\n"
        "Items:\n"
        f"{lines_text}\n\n"
        f"Subtotal: {prefix}{subtotal:.2f}\n"
        f"Shipping: {prefix}{shipping:.2f}\n"
        f"Tax: {prefix}{tax:.2f}\n"
        f"Total: {prefix}{total:.2f}\n\n"
        "Shipping To:\n"
        f"{full_name}\n{email}{(' • ' + phone) if phone else ''}\n"
        f"{address}\n"
    )

    # HTML
    def esc(s):
        return escape(s or "")

    rows = ""
    for l in lines:
        img_td = (
            f'<img src="{esc(l["image"])}" alt="" '
            'style="height:40px;width:40px;object-fit:cover;border-radius:6px;margin-right:8px" />'
        ) if l["image"] else ""

        weight_html = (
            f" <span style='color:#666;font-size:12px'>({esc(l['weight'])})</span>"
            if l["weight"]
            else ""
        )

        rows += (
            "<tr>"
            f'<td style="padding:8px;border:1px solid #eee;vertical-align:top">{img_td}{esc(l["name"])}{weight_html}</td>'
            f'<td style="padding:8px;border:1px solid #eee;text-align:right">{int(l["qty"])}</td>'
            f'<td style="padding:8px;border:1px solid #eee;text-align:right">{prefix}{l["unit"]:.2f}</td>'
            f'<td style="padding:8px;border:1px solid #eee;text-align:right"><strong>{prefix}{l["line_total"]:.2f}</strong></td>'
            "</tr>"
        )

    if not rows:
        rows = (
            '<tr><td colspan="4" '
            'style="padding:12px;border:1px solid #eee;color:#666">No items</td></tr>'
        )

    pay_provider = (
        getattr(getattr(order, "payment", None), "provider", "")
        or getattr(order, "payment_method", "")
        or "-"
    )
    pay_status = (
        getattr(getattr(order, "payment", None), "status", "")
        or ("paid" if order.status == "confirmed" else "unpaid")
    )
    txn_id = getattr(getattr(order, "payment", None), "transaction_id", "") or ""
    # NEW: pick up specific payment method (upi / card / netbanking / ...)
    pay_method = (
        getattr(getattr(order, "payment", None), "method", "")
        or getattr(order, "payment_method", "")
        or ""
    )

    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial,sans-serif;color:#111">
      <h2 style="margin:0 0 8px">{'Thank you for your order!' if heading_for_customer else 'New order received'}</h2>
      <div style="color:#666;margin-bottom:12px">
        Order <strong>#{order.id}</strong> • Status: <strong>{esc(order.status)}</strong> •
        {timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M')}
      </div>

      <table style="width:100%;border-collapse:collapse;margin-top:8px">
        <thead>
          <tr>
            <th style="text-align:left;padding:8px;border:1px solid #eee;background:#fafafa">Item</th>
            <th style="text-align:right;padding:8px;border:1px solid #eee;background:#fafafa">Qty</th>
            <th style="text-align:right;padding:8px;border:1px solid #eee;background:#fafafa">Unit</th>
            <th style="text-align:right;padding:8px;border:1px solid #eee;background:#fafafa">Amount</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>

      <table style="margin-top:12px;margin-left:auto;border-collapse:collapse;min-width:280px">
        <tr><td style="padding:6px 8px;color:#555">Subtotal</td><td style="padding:6px 0;text-align:right">{prefix}{subtotal:.2f}</td></tr>
        <tr><td style="padding:6px 8px;color:#555">Shipping</td><td style="padding:6px 0;text-align:right">{prefix}{shipping:.2f}</td></tr>
        <tr><td style="padding:6px 8px;color:#555">Tax</td><td style="padding:6px 0;text-align:right">{prefix}{tax:.2f}</td></tr>
        <tr><td style="padding:8px 8px;border-top:1px solid #eee"><strong>Total</strong></td><td style="padding:8px 0;border-top:1px solid #eee;text-align:right"><strong>{prefix}{total:.2f}</strong></td></tr>
      </table>

      <div style="display:flex;gap:24px;margin-top:16px">
        <div style="flex:1">
          <h3 style="margin:0 0 6px">Shipping To</h3>
          <div>{esc(full_name)}</div>
          <div>{esc(email)}{' • ' + esc(phone) if phone else ''}</div>
          <div style="white-space:pre-wrap;color:#333">{esc(address)}</div>
        </div>
        <div style="flex:1">
          <h3 style="margin:0 0 6px">Payment</h3>
          <div>Provider: <strong>{esc(pay_provider)}</strong></div>
          {('<div>Method: <strong>' + esc(pay_method) + '</strong></div>') if pay_method else ''}
          <div>Status: <strong style="text-transform:capitalize">{esc(pay_status)}</strong></div>
          {('<div>Txn ID: <span style="font-family:monospace">' + esc(txn_id) + '</span></div>') if txn_id else ''}
        </div>
      </div>

      <div style="margin-top:16px;color:#666">
        If you have any questions, reply to this email.
      </div>
    </div>
    """
    return subject, text, html



def _send_email(subject, to_list, text_body, html_body=None, bcc=None):
    if not to_list:
        return
    msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=_from_email(), to=to_list, bcc=(bcc or []))
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    try:
        msg.send(fail_silently=True)
    except Exception:
        # don’t crash app because of SMTP errors
        pass

def send_order_emails(order, request):
    """Send customer receipt + admin notification."""
    # Customer
    det = getattr(order, "checkout_details", None)
    cust_email = getattr(det, "email", "") if det else ""
    if cust_email:
        s, t, h = _render_order_email_parts(order, request, heading_for_customer=True)
        _send_email(s, [cust_email], t, h)

    # Admins
    admins = _admin_recipients()
    if admins:
        s, t, h = _render_order_email_parts(order, request, heading_for_customer=False)
        s = f"[Admin] {s}"
        _send_email(s, admins, t, h)

# ---------- permissions ----------
class MeView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        u = request.user
        return Response({
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "is_active": u.is_active,
            "is_staff": u.is_staff,
            "is_superuser": u.is_superuser,
            "is_vendor": getattr(u, "is_vendor", False),
        })

class EmailAuthTokenSerializer(serializers.Serializer):
    """
    POST { "email": "user@example.com", "password": "..." }
    -> { "token": "<key>" }
    """
    email = serializers.EmailField()
    password = serializers.CharField()

    default_error_messages = {
        "invalid_creds": "Unable to log in with provided credentials.",
        "disabled": "User account is disabled.",
    }

    def validate(self, attrs):
        email = (attrs.get("email") or "").strip().lower()
        password = attrs.get("password") or ""

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.fail("invalid_creds")

        if not user.check_password(password):
            self.fail("invalid_creds")

        if not user.is_active:
            self.fail("disabled")

        attrs["user"] = user
        return attrs

class EmailObtainAuthToken(ObtainAuthToken):
    authentication_classes = []          # <- CSRF-free (no SessionAuthentication)
    permission_classes = [AllowAny] 
    serializer_class = EmailAuthTokenSerializer
    def post(self, request, *args, **kwargs):
        ser = self.serializer_class(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        user = ser.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        # send login notification (non-blocking best-effort)
        try:
            subj = "New login to your account"
            when = timezone.localtime().strftime("%Y-%m-%d %H:%M")
            ip = request.META.get("REMOTE_ADDR") or "-"
            agent = request.META.get("HTTP_USER_AGENT") or "-"
            text = f"You just logged in at {when}\nIP: {ip}\nAgent: {agent}\n\nIf this wasn't you, please reset your password."
            html = f"""<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial,sans-serif">
              <h3 style="margin:0 0 8px">New login to your account</h3>
              <div>Time: <strong>{when}</strong></div>
              <div>IP: <code>{escape(ip)}</code></div>
              <div>User Agent: <code style="word-break:break-all">{escape(agent)}</code></div>
              <p style="color:#555">If this wasn’t you, please reset your password immediately.</p>
            </div>"""
            _send_email(subj, [user.email], text, html)
        except Exception:
            pass
        return Response({"token": token.key})

class RegisterView(APIView):
    """
    POST {
      "email": "user@example.com",
      "password": "secret123",
      "first_name": "John",
      "last_name": "Doe"
    } -> 201 { "ok": true }
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        data = request.data or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        first = (data.get("first_name") or "").strip()
        last = (data.get("last_name") or "").strip()

        try:
            validate_email(email)
        except DjangoValidationError:
            return Response({"detail": "Invalid email"}, status=status.HTTP_400_BAD_REQUEST)

        if len(password) < 6:
            return Response({"detail": "Password too short (min 6 chars)"}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=email).exists():
            return Response({"detail": "Email already in use"}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create(email=email, first_name=first, last_name=last, is_active=True)
        user.set_password(password)
        user.save(update_fields=["password", "first_name", "last_name"])

        # Welcome/confirmation email (best-effort)
        try:
            subj = "Welcome to our store!"
            name = (user.get_full_name() or user.email or "").strip()
            text = f"Hi {name},\n\nYour account has been created successfully.\n\nHappy shopping!"
            html = f"""<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial,sans-serif">
                <h2 style="margin:0 0 8px">Welcome, {escape(name)} 👋</h2>
                <p>Your account was created successfully. You can now sign in and start shopping.</p>
            </div>"""
            _send_email(subj, [user.email], text, html)
        except Exception:
            pass

        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class IsAdminOrVendorOwner(permissions.BasePermission):
    """
    Staff can do anything.
    Vendors can write only their own products/variants/specs/images.
    Others read-only.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if request.user and request.user.is_staff:
            return True
        if request.user and getattr(request.user, "is_vendor", False):
            return True
        return False

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if request.user and request.user.is_staff:
            return True

        def _vendor_user_id(o):
            if isinstance(o, Product):
                return getattr(o.vendor, "user_id", None)
            if isinstance(o, ProductVariant):
                return getattr(o.product.vendor, "user_id", None)
            if isinstance(o, ProductImage):
                return getattr(o.product.vendor, "user_id", None)
            if isinstance(o, VariantImage):
                return getattr(o.variant.product.vendor, "user_id", None)
            if isinstance(o, ProductSpecification):
                return getattr(o.product.vendor, "user_id", None)
            return None

        return _vendor_user_id(obj) == getattr(request.user, "id", None)

# ---------- simple sets ----------
class SalesSeriesView(APIView):
    permission_classes = [IsAdminUser]  # make AllowAny if you want it public

    def get(self, request):
        gran = (request.query_params.get("granularity") or "month").lower()
        periods = max(1, min(int(request.query_params.get("periods") or 6), 24))

        trunc = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}.get(gran, TruncMonth)
        now = timezone.now()

        # base filters (only paid revenue)
        pay_qs = OrderPayment.objects.filter(status="paid")
        ord_qs = Order.objects.all()

        # group
        pay_series = (
            pay_qs
            .annotate(bucket=trunc("created_at"))
            .values("bucket")
            .annotate(revenue=Sum("amount"))
        )
        ord_series = (
            ord_qs
            .annotate(bucket=trunc("created_at"))
            .values("bucket")
            .annotate(orders=Count("id"), customers=Count("user_id", distinct=True))
        )

        # merge buckets into a dict
        buckets = {}
        for row in pay_series:
            k = row["bucket"].date() if gran == "day" else row["bucket"]
            buckets.setdefault(k, {"revenue": 0, "orders": 0, "customers": 0})
            buckets[k]["revenue"] = float(row["revenue"] or 0)

        for row in ord_series:
            k = row["bucket"].date() if gran == "day" else row["bucket"]
            buckets.setdefault(k, {"revenue": 0, "orders": 0, "customers": 0})
            buckets[k]["orders"] = int(row["orders"] or 0)
            buckets[k]["customers"] = int(row["customers"] or 0)

        # build last N periods, oldest→newest
        def add_months(dt, n):
            m = (dt.month - 1 + n) % 12 + 1
            y = dt.year + (dt.month - 1 + n) // 12
            d = min(dt.day, 28)  # keep simple
            return dt.replace(year=y, month=m, day=d)

        points = []
        if gran == "day":
            start = now.date().replace(day=now.day)  # today
            for i in range(periods - 1, -1, -1):
                day = now.date() - timezone.timedelta(days=i)
                v = buckets.get(day) or {"revenue": 0, "orders": 0, "customers": 0}
                points.append({"name": day.strftime("%d %b"), "sales": v["revenue"], "orders": v["orders"], "customers": v["customers"]})
        elif gran == "week":
            # ISO week label
            for i in range(periods - 1, -1, -1):
                wk_start = (now - timezone.timedelta(weeks=i))
                k = trunc("created_at")(now).resolve_expression(Order.objects.query).output_field  # not used; we’ll just best-effort
                # approximate by monday of that week for lookup
                monday = (now - timezone.timedelta(weeks=i)).date()
                v = buckets.get(monday) or {"revenue": 0, "orders": 0, "customers": 0}
                points.append({"name": f"W{(now.isocalendar().week - i - 1) % 52 + 1}", "sales": v["revenue"], "orders": v["orders"], "customers": v["customers"]})
        else:  # month
            base = now.replace(day=1)
            for i in range(periods - 1, -1, -1):
                month_dt = add_months(base, -i)
                key = month_dt.replace(day=1)
                v = buckets.get(key) or {"revenue": 0, "orders": 0, "customers": 0}
                points.append({"name": month_dt.strftime("%b"), "sales": v["revenue"], "orders": v["orders"], "customers": v["customers"]})

        return Response({"granularity": gran, "points": points})

class DashboardKpiView(APIView):
    permission_classes = [IsAdminUser]  # or AllowAny if you want it public

    def get(self, request):
        # products
        total_products = Product.objects.count()
        in_stock = Product.objects.filter(quantity__gt=0).count()
        out_of_stock = total_products - in_stock

        # orders (customize if you track date fields differently)
        today = timezone.localdate()
        orders_today = Order.objects.filter(created_at__date=today).count()
        orders_month = Order.objects.filter(
            created_at__year=today.year, created_at__month=today.month
        ).count()

        # revenue (example: sum OrderPayment where status=paid)
        revenue_today = (
            OrderPayment.objects
            .filter(status="paid", created_at__date=today)
            .aggregate(x=Sum("amount"))
            .get("x") or 0
        )
        revenue_month = (
            OrderPayment.objects
            .filter(status="paid", created_at__year=today.year, created_at__month=today.month)
            .aggregate(x=Sum("amount"))
            .get("x") or 0
        )

        # avg rating (approved reviews)
        avg_rating = (
            ProductReview.objects.filter(is_approved=True)
            .aggregate(x=Sum("rating") * 1.0 / Count("id"))
            .get("x")
        )
        # if DB above returns None, normalize:
        avg_rating = f"{(avg_rating or 0):.1f}"

        # wishlist items (if you keep per-user wishlists)
        wishlist_items = WishlistItem.objects.count() if WishlistItem.objects.exists() else 0

        data = {
            "totalProducts": total_products,
            "inStock": in_stock,
            "outOfStock": out_of_stock,
            "totalSold": ProductVariant.objects.aggregate(x=Sum("quantity")).get("x") or 0,  # adjust if you store sold separately
            "ordersToday": orders_today,
            "revenueToday": f"₹{int(revenue_today):,}",
            "ordersThisMonth": orders_month,
            "revenueThisMonth": f"₹{int(revenue_month):,}",
            "averageRating": str(avg_rating),
            "wishlistItems": wishlist_items,
        }
        return Response(data)
    
class ColorViewSet(viewsets.ModelViewSet):
    queryset = Color.objects.all().order_by("name")
    serializer_class = ColorSerializer
    permission_classes = [permissions.AllowAny]

class StoreViewSet(viewsets.ModelViewSet):
    queryset = Store.objects.all().order_by("name")
    serializer_class = StoreSerializer

    def get_permissions(self):
        return [permissions.AllowAny()] if self.action in ["list", "retrieve"] else [permissions.IsAdminUser()]

class VendorViewSet(viewsets.ModelViewSet):
    queryset = Vendor.objects.select_related("user", "store").all().order_by("display_name")
    serializer_class = VendorSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        u = self.request.user
        return qs if u.is_staff else qs.filter(user=u)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

# ---------- Category ----------

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.select_related("parent").prefetch_related("children").order_by("name")
    serializer_class = CategorySerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "created_at"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_permissions(self):
        return [permissions.AllowAny()] if self.action in ["list", "retrieve"] else [permissions.IsAuthenticated()]

# ---------- Product & Variants & Specs ----------

class ProductViewSet(viewsets.ModelViewSet):
    queryset = (
        Product.objects
        .select_related("category", "vendor", "store")
        .prefetch_related("images", "variants", "options", "specifications")
        .all()
    )
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "slug", "description", "category__name"]
    ordering_fields = ["created_at", "name", "price_inr", "price_usd"]
    filterset_fields = ["featured", "category", "category__slug", "is_published", "slug"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_permissions(self):
        if self.action in ["list", "retrieve", "track_view"]:
            return [permissions.AllowAny()]
        return [IsAdminOrVendorOwner()]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["country_code"] = _country_code(self.request)
        ctx["request"] = self.request
        return ctx

    def get_serializer_class(self):
        return ProductCreateUpdateSerializer if self.action in ["create", "update", "partial_update", "bulk_upload_images"] else ProductReadSerializer

    def perform_create(self, serializer):
        vendor = None
        if getattr(self.request.user, "is_vendor", False):
            vendor = Vendor.objects.filter(user=self.request.user).first()
        serializer.save(vendor=vendor)

    @action(
        detail=False,
        methods=["get"],
        url_path=r"by-slug/(?P<slug>[-a-z0-9]+)",
        permission_classes=[permissions.AllowAny],
    )
    def by_slug(self, request, slug=None):
        obj = get_object_or_404(self.get_queryset(), slug=slug)
        ser = ProductReadSerializer(obj, context=self.get_serializer_context())
        return Response(ser.data)

    @action(detail=True, methods=["post"], permission_classes=[IsAdminOrVendorOwner])
    def bulk_upload_images(self, request, pk=None):
        """
        Multipart:
          images: file[]    (one or more)
          images_meta: JSON list [{filename, is_primary}]
        """
        product = self.get_object()
        ser = ProductCreateUpdateSerializer(
            instance=product,
            data={},
            context={"request": request},
            partial=True,
        )
        ser.is_valid(raise_exception=True)
        ser._handle_images_from_request(product)
        return Response({"ok": True})

    @action(detail=True, methods=["post"], permission_classes=[permissions.AllowAny])
    def track_view(self, request, pk=None):
        Product.objects.filter(pk=pk).update(views_count=F("views_count") + 1)
        return Response({"ok": True})

    @action(detail=True, methods=["put"], permission_classes=[IsAdminOrVendorOwner])
    def replace_specifications(self, request, pk=None):
        product = self.get_object()
        ser = ProductSpecificationSerializer(data=request.data, many=True)
        ser.is_valid(raise_exception=True)
        ProductSpecification.objects.filter(product=product).delete()
        for i, spec in enumerate(ser.validated_data):
            ProductSpecification.objects.create(
                product=product,
                name=spec["name"],
                value=spec["value"],
                unit=spec.get("unit") or "",
                group=spec.get("group") or "",
                is_highlight=spec.get("is_highlight") or False,
                sort_order=spec.get("sort_order") or i,
            )
        return Response({"ok": True})

    @action(detail=True, methods=["post"], permission_classes=[IsAdminOrVendorOwner])
    def upsert_variants(self, request, pk=None):
        """
        Upsert with de-duplication:
        1) Prefer matching by (weight_value, weight_unit) if provided.
        2) Otherwise match by SKU.
        3) If creating, auto-generate a unique SKU from product.slug + weight (e.g. "slug-500g").
        Payload: { "variants": [ {sku?, weight_value?, weight_unit?, price, stock, is_active, mrp, min_order_qty?, step_qty?} ] }
        """
        product = self.get_object()
        items = request.data.get("variants") or []
        if not isinstance(items, list):
            return Response({"detail": "variants must be a list"}, status=400)

        def _dec(x, default=None):
            if x in (None, "", "null"):
                return default
            try:
                return Decimal(str(x))
            except Exception:
                return default

        def _unique_sku(base: str) -> str:
            base = (base or "").strip() or f"{product.slug}-var"
            candidate = base
            i = 2
            while ProductVariant.objects.filter(product=product, sku=candidate).exists():
                candidate = f"{base}-{i}"
                i += 1
            return candidate

        out = []
        for v in items:
            raw_sku = (v.get("sku") or "").strip()
            wv = _dec(v.get("weight_value"))
            wu = (v.get("weight_unit") or "").strip().upper() or None

            # Prepare defaults
            defaults = dict(
                attributes=v.get("attributes") or ({"Weight": f"{v.get('weight_value')}{(v.get('weight_unit') or '').upper()}"} if (wv is not None and wu) else {}),
                weight_value=wv,
                weight_unit=wu,
                price_override=_dec(v.get("price"), None),
                quantity=int(v.get("stock") or 0),
                is_active=bool(v.get("is_active", True)),
                mrp=_dec(v.get("mrp"), None),
                min_order_qty=int(v.get("min_order_qty") or 1),
                step_qty=int(v.get("step_qty") or 1),
            )

            variant = None

            # 1) Try match by weight combo (if provided)
            if (wv is not None) and wu:
                variant = ProductVariant.objects.filter(
                    product=product, weight_value=wv, weight_unit=wu
                ).first()

            # 2) Else try match by SKU
            if variant is None and raw_sku:
                variant = ProductVariant.objects.filter(product=product, sku=raw_sku).first()

            # 3) Create if none found
            if variant is None:
                # Derive or ensure unique SKU
                if raw_sku:
                    sku_final = _unique_sku(raw_sku)
                elif (wv is not None) and wu:
                    base_sku = f"{product.slug}-{str(wv).rstrip('0').rstrip('.') if '.' in str(wv) else str(wv)}{wu.lower()}"
                    sku_final = _unique_sku(base_sku)
                else:
                    sku_final = _unique_sku(f"{product.slug}-var")

                variant = ProductVariant.objects.create(product=product, sku=sku_final, **defaults)
                out.append(variant)
                continue

            # 4) Update existing (and optionally move SKU if provided)
            if raw_sku:
                if variant.sku != raw_sku:
                    # If another row already has raw_sku, pick a unique one for *this* variant
                    if ProductVariant.objects.filter(product=product, sku=raw_sku).exclude(pk=variant.pk).exists():
                        # keep current variant.sku (no clash) – or you can assign a unique derived one:
                        pass
                    else:
                        variant.sku = raw_sku  # free to use
            # apply field updates
            for k, val in defaults.items():
                setattr(variant, k, val)
            variant.save()
            out.append(variant)

        ser = ProductVariantSerializer(out, many=True, context=self.get_serializer_context())
        return Response({"ok": True, "count": len(out), "variants": ser.data}, status=200)

class ProductImageViewSet(viewsets.ModelViewSet):
    queryset = ProductImage.objects.select_related("product")
    serializer_class = ProductImageSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [IsAdminOrVendorOwner()]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

class ProductVariantViewSet(viewsets.ModelViewSet):
    queryset = ProductVariant.objects.select_related("product", "product__vendor", "color").prefetch_related("images")
    serializer_class = ProductVariantSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [IsAdminOrVendorOwner()]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

class ProductSpecificationViewSet(viewsets.ModelViewSet):
    queryset = ProductSpecification.objects.select_related("product", "product__vendor")
    serializer_class = ProductSpecificationSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [IsAdminOrVendorOwner()]

class VariantImageViewSet(viewsets.ModelViewSet):
    queryset = VariantImage.objects.select_related("variant", "variant__product")
    serializer_class = VariantImageSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [IsAdminOrVendorOwner()]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

# ---------- Cart / Order ----------

class CartViewSet(viewsets.ModelViewSet):
    """
    Endpoints (auth required):
      GET  /api/carts/               -> returns the current user's active cart (single object)
      POST /api/carts/sync/
      POST /api/carts/add_item/
      POST /api/carts/set_quantity/
      POST /api/carts/remove_item/
    """
    queryset = Cart.objects.prefetch_related("items", "items__product", "items__variant", "items__variant__images")
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    serializer_class = CartSerializer  # <-- enable read endpoints

    def _ensure_cart(self, user):
        cart = Cart.objects.filter(user=user, checked_out=False).first()
        if not cart:
            cart = Cart.objects.create(user=user, checked_out=False)
        return cart

    def list(self, request, *args, **kwargs):
        """
        Return only the *active* cart for the current user as a single object,
        not a paginated list (keeps frontend simple).
        """
        cart = Cart.objects.filter(user=request.user, checked_out=False)\
                           .prefetch_related("items", "items__product", "items__variant")\
                           .first()
        if not cart:
            # safe empty shape so UI never crashes
            return Response({"id": None, "checked_out": False, "items": []})
        ser = self.get_serializer(cart)
        return Response(ser.data)

    # ------- keep your existing actions below -------

    def _int(self, v, default=0):
        try:
            return int(v)
        except Exception:
            return default

    @action(detail=False, methods=["post"])
    def sync(self, request):
        user = request.user
        cart = self._ensure_cart(user)
        data = request.data or {}
        lines = data.get("lines") or []
        mode = (data.get("mode") or "replace").lower()
        if mode not in ("replace", "merge"):
            mode = "replace"

        existing = {(ci.product_id, ci.variant_id): ci for ci in cart.items.all()}
        if mode == "replace":
            CartItem.objects.filter(cart=cart).delete()
            existing = {}

        for ln in lines:
            pid = self._int(ln.get("product_id"), 0)
            vid = ln.get("variant_id")
            qty = self._int(ln.get("quantity"), 0)
            if pid <= 0 or qty <= 0:
                continue

            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue

            variant = None
            if vid not in (None, "", "null"):
                try:
                    variant = ProductVariant.objects.get(pk=int(vid), product_id=pid)
                except (ProductVariant.DoesNotExist, ValueError, TypeError):
                    variant = None

            key = (pid, variant.id if variant else None)
            if key in existing:
                ci = existing[key]
                if mode == "replace":
                    ci.quantity = qty
                else:
                    ci.quantity = max(1, ci.quantity + qty)
                ci.save(update_fields=["quantity"])
            else:
                CartItem.objects.create(cart=cart, product=product, variant=variant, quantity=qty)

        cart.refresh_from_db()
        return Response({"ok": True, "items": cart.items.count()})

    @action(detail=False, methods=["post"])
    def add_item(self, request):
        cart = self._ensure_cart(request.user)
        pid = self._int(request.data.get("product_id"), 0)
        vid = request.data.get("variant_id")
        qty = self._int(request.data.get("quantity"), 0)
        if pid <= 0 or qty <= 0:
            return Response({"detail": "Invalid product/quantity"}, status=400)

        try:
            product = Product.objects.get(pk=pid)
        except Product.DoesNotExist:
            return Response({"detail": "Product not found"}, status=404)

        variant = None
        if vid not in (None, "", "null"):
            try:
                variant = ProductVariant.objects.get(pk=int(vid), product_id=pid)
            except (ProductVariant.DoesNotExist, ValueError, TypeError):
                variant = None

        obj, created = CartItem.objects.get_or_create(
            cart=cart, product=product, variant=variant, defaults={"quantity": qty}
        )
        if not created:
            obj.quantity = max(1, obj.quantity + qty)
            obj.save(update_fields=["quantity"])
        return Response({"ok": True, "id": obj.id, "quantity": obj.quantity})

    @action(detail=False, methods=["post"])
    def set_quantity(self, request):
        cart = self._ensure_cart(request.user)
        pid = self._int(request.data.get("product_id"), 0)
        vid = request.data.get("variant_id")
        qty = self._int(request.data.get("quantity"), -1)
        if pid <= 0 or qty < 0:
            return Response({"detail": "Invalid product/quantity"}, status=400)

        variant_id = None
        if vid not in (None, "", "null"):
            try:
                variant_id = int(vid)
            except Exception:
                variant_id = None

        ci = CartItem.objects.filter(cart=cart, product_id=pid, variant_id=variant_id).first()
        if not ci:
            return Response({"detail": "Cart item not found"}, status=404)

        if qty == 0:
            ci.delete()
            return Response({"ok": True, "removed": True})

        ci.quantity = qty
        ci.save(update_fields=["quantity"])
        return Response({"ok": True, "quantity": ci.quantity})

    @action(detail=False, methods=["post"])
    def remove_item(self, request):
        cart = self._ensure_cart(request.user)
        pid = self._int(request.data.get("product_id"), 0)
        vid = request.data.get("variant_id")
        if pid <= 0:
            return Response({"detail": "Invalid product"}, status=400)

        variant_id = None
        if vid not in (None, "", "null"):
            try:
                variant_id = int(vid)
            except Exception:
                variant_id = None

        CartItem.objects.filter(cart=cart, product_id=pid, variant_id=variant_id).delete()
        return Response({"ok": True, "removed": True})



class OrderViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = OrderSerializer

    def _base_queryset(self):
        return (
            Order.objects
            .select_related("cart", "user")
            .prefetch_related(
                "cart__items",
                "cart__items__product",
                "cart__items__product__category",
                "cart__items__variant",
                "cart__items__variant__images",
            )
            .order_by("-created_at")
        )

    queryset = None

    def get_queryset(self):
        qs = self._base_queryset()
        user = self.request.user

        # Normalize ?mine flag
        mine_raw = str(self.request.query_params.get("mine", "")).strip().lower()
        mine = mine_raw in ("1", "true", "yes")

        # Own orders explicitly
        if mine:
            return qs.filter(user=user)

        # Staff sees all
        if user.is_authenticated and (user.is_staff or user.is_superuser):
            return qs

        # Normal users see only theirs
        return qs.filter(user=user)

    # ---------- helpers ----------
    def _get_or_create_guest_user(self, data: dict) -> "User":
        email = (data.get("email") or "").strip().lower()
        first = (data.get("firstName") or "").strip()
        last  = (data.get("lastName") or "").strip()
        if not email:
            raise ValidationError("Email is required")
        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValidationError("Invalid email")

        user, created = User.objects.get_or_create(
            email=email, defaults={"first_name": first, "last_name": last, "is_active": True}
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password", "first_name", "last_name"])
        else:
            updates = {}
            if first and not user.first_name: updates["first_name"] = first
            if last and not user.last_name:   updates["last_name"]  = last
            if updates:
                for k, v in updates.items():
                    setattr(user, k, v)
                user.save(update_fields=list(updates.keys()))
        return user

    def _ensure_cart(self, user: "User") -> "Cart":
        cart = Cart.objects.filter(user=user, checked_out=False).first()
        if not cart:
            cart = Cart.objects.create(user=user, checked_out=False)
        return cart

    def _clean_int(self, v, default=0):
        try:
            return int(v)
        except Exception:
            return default

    def _upsert_cart_items_from_lines(self, cart: "Cart", lines) -> None:
        if not isinstance(lines, list):
            return
        for ln in lines:
            pid = self._clean_int(ln.get("product_id"), 0)
            vid = ln.get("variant_id")
            qty = self._clean_int(ln.get("quantity") or ln.get("qty"), 0)
            if pid <= 0 or qty <= 0:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            variant = None
            if vid not in (None, "", "null"):
                try:
                    variant = ProductVariant.objects.get(pk=int(vid), product_id=pid)
                except (ProductVariant.DoesNotExist, ValueError, TypeError):
                    variant = None
            obj, created = CartItem.objects.get_or_create(
                cart=cart, product=product, variant=variant, defaults={"quantity": qty}
            )
            if not created:
                new_qty = obj.quantity + qty
                if new_qty > 0:
                    obj.quantity = new_qty
                    obj.save(update_fields=["quantity"])
                else:
                    obj.delete()

    # ---------- CREATE / EMAIL ----------
    def perform_create(self, serializer):
        order = serializer.save(
            user=self.request.user,
            status="pending",
            shipment_status="pending",
        )
        if getattr(order, "cart", None):
            order.cart.checked_out = True
            order.cart.save(update_fields=["checked_out"])
        send_order_emails(order, self.request)

    # ---------- CONFIRM ----------
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def confirm(self, request, pk=None):
        order = self.get_object()

        # 1) Confirm order + decrement stock, but never crash API
        try:
            if hasattr(order, "confirm_and_decrement_stock"):
                order.confirm_and_decrement_stock()
            else:
                # Fallback: just mark confirmed if method not present
                if order.status != "confirmed":
                    order.status = "confirmed"
                    order.save(update_fields=["status"])
        except Exception as e:
            # If stock op fails, still move to confirmed? choose policy:
            # Here we confirm but log & notify admin, and continue.
            order.status = "confirmed"
            order.save(update_fields=["status"])
            try:
                _send_email(
                    f"[Admin] Order #{order.id} confirm_and_decrement_stock() failed",
                    _admin_recipients(),
                    f"Exception: {e}",
                    None,
                )
            except Exception:
                pass

        # 2) Mark COD payments as paid (robust payment lookup)
        pay = getattr(order, "payment", None)
        if not pay:
            pay = (
                OrderPayment.objects.filter(order=order)
                .order_by("-created_at")
                .first()
            )

        def _lower(x): return (str(x or "")).strip().lower()

        is_cod = False
        if pay:
            is_cod = (
                _lower(pay.method) == "cash-on-delivery"
                or _lower(pay.provider) in ("cod", "cash-on-delivery")
            )
        else:
            is_cod = _lower(order.payment_method) in ("cash-on-delivery", "cod")

        if pay and is_cod and _lower(pay.status) != "paid":
            pay.status = "paid"
            pay.save(update_fields=["status"])

        # 3) Send emails (best-effort)
        try:
            send_order_emails(order, request)
        except Exception:
            pass

        ser = self.get_serializer(order)
        return Response(ser.data, status=200)

    # ---------- COD ----------
    @action(detail=False, methods=["post"], permission_classes=[permissions.AllowAny])
    @transaction.atomic
    def cod(self, request):
        data = request.data or {}
        user = request.user if (request.user and request.user.is_authenticated) else self._get_or_create_guest_user(data)
        cart = self._ensure_cart(user)
        client_lines = data.get("lines") or []
        self._upsert_cart_items_from_lines(cart, client_lines)

        order = Order.objects.create(
            user=user, cart=cart, status="pending", shipment_status="pending",
            payment_method="cash-on-delivery", country_code="IN", currency="INR",
        )

        full_name = (f"{data.get('firstName','').strip()} {data.get('lastName','').strip()}".strip()
                     or user.get_full_name() or user.email)
        OrderCheckoutDetails.objects.create(
            order=order,
            full_name=full_name,
            email=data.get("email",""),
            phone=data.get("phone",""),
            address1=data.get("address",""),
            address2=data.get("address2",""),
            city=data.get("city",""),
            state=data.get("state",""),
            postcode=data.get("zipCode",""),
            country=data.get("country","India") or "India",
            notes=(data.get("notes") or ""),
        )

        totals = data.get("totals") or {}
        OrderPayment.objects.create(
            order=order, method="cash-on-delivery", provider="cod", status="pending",
            transaction_id="", currency="INR",
            amount=Decimal(str(totals.get("grand_total") or totals.get("total") or 0)),
            raw={"lines": client_lines, "totals": totals},
        )

        cart.checked_out = True
        cart.save(update_fields=["checked_out"])

        try:
            send_order_emails(order, request)
        except Exception:
            pass

        ser = self.get_serializer(order)
        return Response({"ok": True, "order": ser.data}, status=201)

    # ---------- Razorpay ----------
     # ---------- Razorpay ----------
    @action(detail=False, methods=["post"], permission_classes=[permissions.AllowAny])
    @transaction.atomic
    def razorpay_confirm(self, request):
        rp_order_id = (request.data or {}).get("razorpay_order_id")
        rp_payment_id = (request.data or {}).get("razorpay_payment_id")
        checkout = (request.data or {}).get("checkout") or {}
        if not rp_order_id or not rp_payment_id:
            return Response({"detail": "razorpay_order_id and razorpay_payment_id required"}, status=400)

        user = (
            request.user
            if (request.user and request.user.is_authenticated)
            else self._get_or_create_guest_user(checkout)
        )
        cart = self._ensure_cart(user)
        client_lines = checkout.get("lines") or []
        self._upsert_cart_items_from_lines(cart, client_lines)

        # --- create base order (status confirmed, shipment pending) ---
        order = Order.objects.create(
            user=user,
            cart=cart,
            status="confirmed",
            shipment_status="pending",
            payment_method="card",  # provisional; will override with Razorpay method below
            country_code="IN",
            currency="INR",
        )

        full_name = (
            f"{checkout.get('firstName','').strip()} {checkout.get('lastName','').strip()}".strip()
            or user.get_full_name()
            or user.email
        )
        OrderCheckoutDetails.objects.create(
            order=order,
            full_name=full_name,
            email=checkout.get("email", ""),
            phone=checkout.get("phone", ""),
            address1=checkout.get("address", ""),
            address2=checkout.get("address2", ""),
            city=checkout.get("city", ""),
            state=checkout.get("state", ""),
            postcode=checkout.get("zipCode", ""),
            country=checkout.get("country", "India") or "India",
            notes=(checkout.get("notes") or ""),
        )

        # --- Fetch Razorpay payment to know METHOD (upi, card, netbanking...) + exact amount ---
        payment_obj = None
        payment_method = "card"
        amount_dec = Decimal("0.00")

        try:
            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            payment_obj = client.payment.fetch(rp_payment_id)

            # e.g. "upi", "card", "netbanking", "wallet", ...
            payment_method = (payment_obj.get("method") or "card").lower()

            # Razorpay sends amount in paise -> convert to rupees
            amt_paise = Decimal(str(payment_obj.get("amount") or "0"))
            amount_dec = (amt_paise / Decimal("100")).quantize(Decimal("0.01"))
        except Exception:
            # Fallback: use amount from request if provided
            try:
                amount_dec = Decimal(str(request.data.get("amount") or 0))
            except Exception:
                amount_dec = Decimal("0.00")

        # Update order with the actual Razorpay method (upi/card/netbanking/etc.)
        order.payment_method = payment_method
        order.save(update_fields=["payment_method"])

        OrderPayment.objects.create(
            order=order,
            method=payment_method,             # <- upi / card / netbanking / ...
            provider="razorpay",
            status="paid",
            transaction_id=rp_payment_id,
            currency="INR",
            amount=amount_dec,
            raw={
                "razorpay_order_id": rp_order_id,
                "razorpay_payment_id": rp_payment_id,
                "razorpay_payment": payment_obj,   # full Razorpay payment payload for debugging
                "lines": client_lines,
                "totals": checkout.get("totals") or {},
            },
        )

        cart.checked_out = True
        cart.save(update_fields=["checked_out"])

        # Try stock decrement, but never 500
        try:
            if hasattr(order, "confirm_and_decrement_stock"):
                order.confirm_and_decrement_stock()
            else:
                if order.status != "confirmed":
                    order.status = "confirmed"
                    order.save(update_fields=["status"])
        except Exception as e:
            order.status = "pending"
            order.save(update_fields=["status"])
            try:
                _send_email(
                    f"[Admin] Order #{order.id} stock confirmation failed",
                    _admin_recipients(),
                    f"Order #{order.id} confirm_and_decrement_stock() raised: {e}",
                    None,
                )
            except Exception:
                pass
            return Response({"ok": False, "order_id": order.id, "detail": str(e)}, status=409)

        try:
            send_order_emails(order, request)
        except Exception:
            pass

        ser = self.get_serializer(order)
        return Response({"ok": True, "order": ser.data}, status=201)

    # ---------- shipment quick update ----------
    def partial_update(self, request, *args, **kwargs):
        if not request.user.is_staff:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data or {}
        if "shipment_status" not in data:
            return super().partial_update(request, *args, **kwargs)

        val = str(data["shipment_status"])
        valid = dict(Order.SHIPMENT_STATUS_CHOICES)
        if val not in valid:
            return Response({"detail": "Invalid shipment_status"}, status=status.HTTP_400_BAD_REQUEST)

        order = self.get_object()
        if order.shipment_status != val:
            order.shipment_status = val
            order.save(update_fields=["shipment_status"])

        ser = self.get_serializer(order)
        return Response(ser.data, status=200)

    @action(detail=True, methods=["post"])
    def set_shipment(self, request, pk=None):
        if not request.user.is_staff:
            return Response({"detail": "Forbidden"}, status=403)
        val = str((request.data or {}).get("shipment_status") or "")
        valid = dict(Order.SHIPMENT_STATUS_CHOICES)
        if val not in valid:
            return Response({"detail": "Invalid shipment_status"}, status=400)
        order = self.get_object()
        order.shipment_status = val
        order.save(update_fields=["shipment_status"])
        return Response({"id": order.id, "shipment_status": order.shipment_status})

# ---------- Payments: Razorpay ----------
class RazorpayPaymentViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]
    parser_classes     = [JSONParser, FormParser]

    def _client(self):
        key_id     = settings.RAZORPAY_KEY_ID
        key_secret = settings.RAZORPAY_KEY_SECRET
        if not key_id or not key_secret:
            raise RuntimeError("Razorpay credentials missing. Set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET.")
        return razorpay.Client(auth=(key_id, key_secret))

    @action(detail=False, methods=["post"])
    def create_order(self, request):
        data = request.data or {}
        amount_rupees = data.get("amount")
        amount_paise  = data.get("amount_paise")
        currency      = (data.get("currency") or "INR").upper()
        receipt       = data.get("receipt") or f"rcpt_{int(timezone.now().timestamp())}"
        notes         = data.get("notes") or {}

        try:
            if amount_paise is None:
                amt = Decimal(str(amount_rupees or "0"))
                if amt <= 0:
                    return Response({"detail": "amount must be > 0"}, status=400)
                amount_paise = int(amt * 100)
            else:
                amount_paise = int(amount_paise)
        except Exception:
            return Response({"detail": "Invalid amount/amount_paise"}, status=400)

        client = self._client()
        order  = client.order.create({
            "amount": amount_paise, "currency": currency, "receipt": receipt,
            "payment_capture": 1, "notes": notes,
        })
        return Response({"order": order, "key_id": settings.RAZORPAY_KEY_ID}, status=201)

    @action(detail=False, methods=["post"])
    def verify(self, request):
        data = request.data or {}
        order_id   = data.get("razorpay_order_id")
        payment_id = data.get("razorpay_payment_id")
        signature  = data.get("razorpay_signature")

        if not (order_id and payment_id and signature):
            return Response({"detail": "Missing fields"}, status=400)

        client = self._client()
        try:
            client.utility.verify_payment_signature({
                "razorpay_order_id":   str(order_id),
                "razorpay_payment_id": str(payment_id),
                "razorpay_signature":  str(signature),
            })
        except razorpay.errors.SignatureVerificationError:
            return Response({"ok": False, "detail": "Signature verification failed"}, status=400)

        return Response({"ok": True}, status=200)
class RazorpayBase(APIView):
    """
    Base APIView that disables SessionAuthentication (so no CSRF),
    and allows public access for the widget callbacks.
    """
    authentication_classes: list[type[BaseAuthentication]] = []
    permission_classes = [AllowAny]

    @property
    def _client(self):
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            raise ValidationError("Razorpay keys not configured")
        return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


class RazorpayCreateOrder(RazorpayBase):
    """
    POST { amount: <rupees float|int>, currency: "INR", receipt?: str, notes?: dict }
    -> { order: {...}, key_id: "<public key>" }
    """
    def post(self, request):
        data = request.data or {}
        try:
            amount_rupees = float(data.get("amount", 0))
        except Exception:
            raise ValidationError("Invalid amount")
        if amount_rupees <= 0:
            raise ValidationError("Amount must be > 0")

        currency = (data.get("currency") or "INR").upper()
        if currency != "INR":
            raise ValidationError("Only INR supported")

        amount_paise = int(round(amount_rupees * 100))
        receipt = data.get("receipt") or f"rcpt_{int(timezone.now().timestamp())}"
        notes = data.get("notes") or {}

        order = self._client.order.create(
            dict(
                amount=amount_paise,
                currency=currency,
                receipt=receipt,
                notes=notes,
                payment_capture=1,  # capture automatically
            )
        )
        return Response({"order": order, "key_id": settings.RAZORPAY_KEY_ID})


class RazorpayVerifyPayment(RazorpayBase):
    """
    POST { razorpay_order_id, razorpay_payment_id, razorpay_signature }
    -> { ok: true }
    """
    def post(self, request):
        rp_order_id = request.data.get("razorpay_order_id")
        rp_payment_id = request.data.get("razorpay_payment_id")
        rp_signature = request.data.get("razorpay_signature")

        if not all([rp_order_id, rp_payment_id, rp_signature]):
            raise ValidationError("Missing fields")

        # Use Razorpay utility for verification
        params = {
            "razorpay_order_id": rp_order_id,
            "razorpay_payment_id": rp_payment_id,
            "razorpay_signature": rp_signature,
        }
        try:
            self._client.utility.verify_payment_signature(params)
        except razorpay.errors.SignatureVerificationError:
            return Response({"ok": False}, status=400)

        return Response({"ok": True})
# ---------- Wishlist ----------

class WishlistViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WishlistSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        wl, _ = Wishlist.create_for_user(self.request.user)
        return Wishlist.objects.filter(pk=wl.pk)

class WishlistItemViewSet(viewsets.ModelViewSet):
    queryset = WishlistItem.objects.select_related("wishlist", "product", "variant")
    serializer_class = WishlistItemSerializer
    permission_classes = [permissions.IsAuthenticated]

# ---------- Contact / Reviews / Visits ----------

class ContactSubmissionViewSet(viewsets.ModelViewSet):
    queryset = ContactSubmission.objects.all().order_by("-created_at")
    serializer_class = ContactSubmissionSerializer
    permission_classes = [permissions.AllowAny]

# (First) ProductReviewViewSet — kept as-is for public usage
class ProductReviewViewSet(viewsets.ModelViewSet):
    """
    Public can list/create; staff can update/delete/moderate.
    Supports ?product=<id>&is_approved=true&ordering=-created_at
    """
    queryset = ProductReview.objects.select_related("product", "user").all()
    serializer_class = ProductReviewSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "comment", "user__email", "product__name"]
    filterset_fields = ["product", "is_approved"]
    ordering_fields = ["created_at", "rating", "id"]
    ordering = ["-created_at"]

    def get_permissions(self):
        # list/retrieve/create: open; update/partial_update/destroy: staff only
        if self.action in ["list", "retrieve", "create"]:
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset()
        # non-staff listing only sees approved unless explicitly filtered
        if not (self.request.user and self.request.user.is_staff):
            # if client doesn't specify is_approved, force true for public
            if "is_approved" not in self.request.query_params:
                qs = qs.filter(is_approved=True)
        return qs
# Visit events
class VisitEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = VisitEvent.objects.select_related("user").order_by("-created_at")
    permission_classes = [permissions.IsAdminUser]

# ---------- Marketing / Blog / Jobs ----------

class PromoBannerViewSet(viewsets.ModelViewSet):
    queryset = PromoBanner.objects.all()
    serializer_class = PromoBannerSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    # Make list/retrieve public so homepage can fetch banners
    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]

class ProductGridViewSet(viewsets.ModelViewSet):
    queryset = ProductGrid.objects.all()
    serializer_class = ProductGridSerializer
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

class SpecialOfferViewSet(viewsets.ModelViewSet):
    queryset = SpecialOffer.objects.all()
    serializer_class = SpecialOfferSerializer
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

class ProductCollectionViewSet(viewsets.ModelViewSet):
    queryset = ProductCollection.objects.all()
    serializer_class = ProductCollectionSerializer
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

class BlogCategoryViewSet(viewsets.ModelViewSet):
    queryset = BlogCategory.objects.all().order_by("name")
    serializer_class = BlogCategorySerializer

    def get_permissions(self):
        return [permissions.AllowAny()] if self.action in ["list", "retrieve"] else [permissions.IsAdminUser()]


class BlogPostViewSet(viewsets.ModelViewSet):
    queryset = BlogPost.objects.select_related("category", "author").all()
    serializer_class = BlogPostSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "excerpt", "content_html", "content_markdown", "tags_csv", "category__name"]
    ordering_fields = ["published_at", "created_at", "views_count", "title"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_permissions(self):
        # public can list/retrieve; writes require staff
        if self.action in ["list", "retrieve", "by_slug", "featured", "increment_view"]:
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset()
        # public: only published
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(is_published=True, published_at__lte=timezone.now())
        # filters
        cat = self.request.query_params.get("category")
        cat_slug = self.request.query_params.get("category_slug")
        tag = self.request.query_params.get("tag")
        if cat:
            qs = qs.filter(category_id=cat)
        if cat_slug:
            qs = qs.filter(category__slug=cat_slug)
        if tag:
            qs = qs.filter(tags_csv__icontains=tag)
        return qs

    @action(detail=False, methods=["get"], url_path=r"by-slug/(?P<slug>[-a-z0-9]+)")
    def by_slug(self, request, slug=None):
        obj = get_object_or_404(self.get_queryset(), slug=slug)
        ser = self.get_serializer(obj)
        return Response(ser.data)

    @action(detail=False, methods=["get"])
    def featured(self, request):
        qs = self.get_queryset().filter(featured=True)[:10]
        ser = self.get_serializer(qs, many=True)
        return Response(ser.data)

    @action(detail=True, methods=["post"])
    def increment_view(self, request, pk=None):
        BlogPost.objects.filter(pk=pk).update(views_count=F("views_count") + 1)
        return Response({"ok": True})

class JobOpeningViewSet(viewsets.ModelViewSet):
    queryset = JobOpening.objects.all()
    permission_classes = [permissions.AllowAny]

class JobApplicationViewSet(viewsets.ModelViewSet):
    queryset = JobApplication.objects.select_related("job")
    permission_classes = [permissions.IsAdminUser]

# (Second copy you had) rename to avoid class redefinition while preserving behavior
class ProductReviewModerationViewSet(viewsets.ModelViewSet):
    queryset = ProductReview.objects.select_related("product", "user").all()
    serializer_class = ProductReviewSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve", "create"]:
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        qs = super().get_queryset()
        product_id = self.request.query_params.get("product")
        if product_id:
            qs = qs.filter(product_id=product_id)

        is_staff = bool(getattr(self.request.user, "is_staff", False))
        approved = self.request.query_params.get("approved")

        if approved is None:
            if not is_staff:
                qs = qs.filter(is_approved=True)
        else:
            if is_staff:
                qs = qs.filter(is_approved=(approved not in ["0", "false", "False"]))
            else:
                qs = qs.filter(is_approved=True)

        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def approve(self, request, pk=None):
        review = self.get_object()
        review.is_approved = True
        review.save(update_fields=["is_approved", "updated_at"])
        return Response({"ok": True}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def unapprove(self, request, pk=None):
        review = self.get_object()
        review.is_approved = False
        review.save(update_fields=["is_approved", "updated_at"])
        return Response({"ok": True}, status=status.HTTP_200_OK)





class PublicReadAdminWriteMixin:
    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx


class TestimonialViewSet(PublicReadAdminWriteMixin, viewsets.ModelViewSet):
    queryset = Testimonial.objects.all().order_by("sort", "-created_at")
    serializer_class = TestimonialSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get("active")
        if self.action in ["list", "retrieve"]:
            if active is None:
                qs = qs.filter(is_active=True)
            else:
                qs = qs.filter(is_active=(active not in ["0", "false", "False"]))
        return qs


class VideoTestimonialViewSet(PublicReadAdminWriteMixin, viewsets.ModelViewSet):
    queryset = VideoTestimonial.objects.all().order_by("sort", "-created_at")
    serializer_class = VideoTestimonialSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get("active")
        if self.action in ["list", "retrieve"]:
            if active is None:
                qs = qs.filter(is_active=True)
            else:
                qs = qs.filter(is_active=(active not in ["0", "false", "False"]))
        return qs


class AwardRecognitionViewSet(PublicReadAdminWriteMixin, viewsets.ModelViewSet):
    queryset = AwardRecognition.objects.all().order_by("sort", "-created_at")
    serializer_class = AwardRecognitionSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get("active")
        if self.action in ["list", "retrieve"]:
            if active is None:
                qs = qs.filter(is_active=True)
            else:
                qs = qs.filter(is_active=(active not in ["0", "false", "False"]))
        return qs


class CertificationViewSet(PublicReadAdminWriteMixin, viewsets.ModelViewSet):
    queryset = Certification.objects.all().order_by("sort", "-created_at")
    serializer_class = CertificationSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get("active")
        if self.action in ["list", "retrieve"]:
            if active is None:
                qs = qs.filter(is_active=True)
            else:
                qs = qs.filter(is_active=(active not in ["0", "false", "False"]))
        return qs


class GalleryItemViewSet(PublicReadAdminWriteMixin, viewsets.ModelViewSet):
    queryset = GalleryItem.objects.all().order_by("category", "sort", "-created_at")
    serializer_class = GalleryItemSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        active = self.request.query_params.get("active")
        if self.action in ["list", "retrieve"]:
            if active is None:
                qs = qs.filter(is_active=True)
            else:
                qs = qs.filter(is_active=(active not in ["0", "false", "False"]))
        return qs
