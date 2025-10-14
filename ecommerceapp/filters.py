import django_filters as df
from .models import *

class ProductFilter(df.FilterSet):
    min_price = df.NumberFilter(field_name="price", lookup_expr="gte")
    max_price = df.NumberFilter(field_name="price", lookup_expr="lte")
    category  = df.CharFilter(method="filter_category")
    search    = df.CharFilter(field_name="name", lookup_expr="icontains")
    featured      = df.BooleanFilter()
    new_arrival   = df.BooleanFilter()
    in_stock      = df.BooleanFilter()

    # Variant attribute filter (e.g., ?attr_name=Color&attr_value=Blue)
    attr_name  = df.CharFilter(method="filter_attr")
    attr_value = df.CharFilter(method="filter_attr")

    class Meta:
        model = Product
        fields = ["featured", "new_arrival", "in_stock"]

    def filter_category(self, qs, name, value):
        try:
            root = Category.objects.get(slug=value)
        except Category.DoesNotExist:
            return qs.none()
        ids = {root.id}
        todo = [root]
        while todo:
            children = Category.objects.filter(parent__in=todo).only("id")
            ids.update([c.id for c in children])
            todo = list(children)
        return qs.filter(category_id__in=ids)

    def filter_attr(self, qs, name, value):
        params = self.data
        attr_name = params.get("attr_name")
        attr_value = params.get("attr_value")
        if not attr_name or not attr_value:
            return qs
        return qs.filter(
            variants__is_active=True,
            variants__attributes__has_key=attr_name,
            variants__attributes__contains={attr_name: attr_value},
        ).distinct()
