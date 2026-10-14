from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from accounts.permissions import IsAdministratorOrReadOnly

from . import services
from .models import AddOn, BoothVariant, MarketWeekend
from .serializers import AddOnSerializer, BoothVariantSerializer, MarketWeekendSerializer


class MarketWeekendViewSet(viewsets.ModelViewSet):
    """Weekends, with their booth variants and add-ons nested under
    /market-weekends/{id}/variants/ and /market-weekends/{id}/add-ons/."""

    serializer_class = MarketWeekendSerializer
    permission_classes = [IsAdministratorOrReadOnly]
    filterset_fields = ['status']
    ordering_fields = ['start_date', 'created_at']
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return MarketWeekend.objects.prefetch_related('variants', 'add_ons')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['usage'] = getattr(self, '_usage', {})
        return context

    def _with_usage(self, weekends):
        self._usage = services.usage_for_weekends(weekends)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        self._with_usage(page)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        weekend = self.get_object()
        self._with_usage([weekend])
        return Response(self.get_serializer(weekend).data)

    def perform_destroy(self, instance):
        services.delete_weekend(instance)

    # --- Nested products -----------------------------------------------------

    def _product_collection(self, request, model, serializer_class):
        weekend = self.get_object()
        context = {**self.get_serializer_context(), 'weekend': weekend}
        if request.method == 'POST':
            serializer = serializer_class(data=request.data, context=context)
            serializer.is_valid(raise_exception=True)
            serializer.save(weekend=weekend)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        context['usage'] = services.usage_for_weekends([weekend])
        products = model.objects.filter(weekend=weekend)
        return Response(serializer_class(products, many=True, context=context).data)

    def _product_detail(self, request, model, serializer_class, product_id):
        weekend = self.get_object()
        product = get_object_or_404(model, pk=product_id, weekend=weekend)
        context = {**self.get_serializer_context(), 'weekend': weekend}
        if request.method == 'DELETE':
            services.delete_product(product)
            return Response(status=status.HTTP_204_NO_CONTENT)
        if request.method == 'PATCH':
            serializer = serializer_class(product, data=request.data, partial=True, context=context)
            serializer.is_valid(raise_exception=True)
            services.check_capacity_change(product, serializer.validated_data.get('capacity', product.capacity))
            serializer.save()
        context['usage'] = services.usage_for_weekends([weekend])
        return Response(serializer_class(product, context=context).data)

    @action(detail=True, methods=['get', 'post'], url_path='variants')
    def variants(self, request, pk=None):
        return self._product_collection(request, BoothVariant, BoothVariantSerializer)

    @action(detail=True, methods=['get', 'patch', 'delete'], url_path=r'variants/(?P<variant_id>[^/.]+)')
    def variant_detail(self, request, pk=None, variant_id=None):
        return self._product_detail(request, BoothVariant, BoothVariantSerializer, variant_id)

    @action(detail=True, methods=['get', 'post'], url_path='add-ons')
    def add_ons(self, request, pk=None):
        return self._product_collection(request, AddOn, AddOnSerializer)

    @action(detail=True, methods=['get', 'patch', 'delete'], url_path=r'add-ons/(?P<add_on_id>[^/.]+)')
    def add_on_detail(self, request, pk=None, add_on_id=None):
        return self._product_detail(request, AddOn, AddOnSerializer, add_on_id)
