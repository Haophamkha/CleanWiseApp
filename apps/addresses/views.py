from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.response import Response

from apps.common.permissions import IsCustomerRole

from .address_service import set_default_address, soft_delete_address
from .models import CustomerAddress
from .serializers import CustomerAddressSerializer


class CustomerAddressListCreateView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = CustomerAddressSerializer

    def get_queryset(self):
        return CustomerAddress.objects.filter(
            customer=self.request.user, is_active=True,
        ).order_by('-created_at', '-id')    

    def get(self, request, *args, **kwargs):
        return Response({'message': 'Lấy danh sách địa điểm thành công.', 'data': self.get_serializer(self.get_queryset(), many=True).data})

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = serializer.save()
        return Response({'message': 'Thêm địa điểm thành công.', 'data': self.get_serializer(address).data}, status=status.HTTP_201_CREATED)


class CustomerAddressDetailView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = CustomerAddressSerializer

    def get_object(self):
        return get_object_or_404(CustomerAddress.objects, pk=self.kwargs['pk'], customer=self.request.user, is_active=True)

    def get(self, request, *args, **kwargs):
        return Response({'message': 'Lấy chi tiết địa điểm thành công.', 'data': self.get_serializer(self.get_object()).data})

    def patch(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        address = serializer.save()
        return Response({'message': 'Cập nhật địa điểm thành công.', 'data': self.get_serializer(address).data})

    def delete(self, request, *args, **kwargs):
        soft_delete_address(self.get_object())
        return Response({'message': 'Xóa địa điểm thành công.'})


class CustomerAddressSetDefaultView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = CustomerAddressSerializer

    def get_object(self):
        return get_object_or_404(CustomerAddress.objects, pk=self.kwargs['pk'], customer=self.request.user, is_active=True)

    def patch(self, request, *args, **kwargs):
        address = set_default_address(self.get_object())
        return Response({'message': 'Đặt địa điểm mặc định thành công.', 'data': self.get_serializer(address).data})
