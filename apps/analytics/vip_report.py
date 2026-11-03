from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone
from rest_framework import serializers

from apps.bookings.models import Booking
from apps.vouchers.models import Voucher
from apps.vouchers.voucher_service import assign_voucher_to_customer
from .favorite_report import FavoriteReportParams
from .models import MonthlyVipReward
from .report_service import ZONE, money, next_month


class VipReportParams(FavoriteReportParams):
    min_bookings = serializers.IntegerField(min_value=1, max_value=10000, default=5)
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=5)


class VipRewardInput(VipReportParams):
    voucher_id = serializers.IntegerField(min_value=1)
    customer_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=50)

    def validate_customer_ids(self, value):
        return sorted(set(value))


def report_month(params):
    today = timezone.localdate(timezone=ZONE)
    return date(params.get('year', today.year), params.get('month', today.month), 1)


def eligible_customers(params):
    month = report_month(params)
    lower = datetime.combine(month, time.min, tzinfo=ZONE)
    upper = datetime.combine(next_month(month), time.min, tzinfo=ZONE)
    return Booking.objects.filter(
        created_at__gte=lower, created_at__lt=upper, status='COMPLETED', payment_status='PAID',
        refunded_amount=0, customer__role='CUSTOMER', customer__is_active=True,
    ).order_by().values('customer_id', 'customer__first_name', 'customer__last_name', 'customer__username').annotate(
        booking_count=Count('id'), completed_value=Sum('total_amount'), last_booking_at=Max('created_at'),
    ).filter(booking_count__gte=params['min_bookings']).order_by('-booking_count', '-completed_value', 'customer_id')


def monthly_vip_report(params):
    month = report_month(params)
    customers = eligible_customers(params)
    total = customers.count()
    pages = max(1, (total + params['page_size'] - 1) // params['page_size'])
    if params['page'] > pages:
        raise serializers.ValidationError({'page': 'Trang không tồn tại.'})
    rewarded = MonthlyVipReward.objects.filter(month=month, customer_id__in=customers.values('customer_id'))
    rewarded_count = rewarded.count()
    offset = (params['page'] - 1) * params['page_size']
    rows = list(customers[offset:offset + params['page_size']])
    rewards = {r.customer_id: r for r in rewarded.filter(customer_id__in=[r['customer_id'] for r in rows]).select_related('user_voucher__voucher')}
    result = []
    for row in rows:
        reward = rewards.get(row['customer_id'])
        result.append({
            'customer_id': row['customer_id'],
            'name': f"{row['customer__first_name']} {row['customer__last_name']}".strip() or row['customer__username'],
            'username': row['customer__username'], 'booking_count': row['booking_count'],
            'completed_value': money(row['completed_value']), 'last_booking_at': row['last_booking_at'],
            'rewarded': reward is not None,
            'reward_code': reward.user_voucher.voucher.code if reward else None,
        })
    return {'month': month.isoformat(), 'timezone': str(ZONE),
            'basis': 'created_in_month_completed_paid_not_refunded',
            'summary': {'customers': total, 'rewarded': rewarded_count, 'unrewarded': total - rewarded_count},
            'results': result, 'count': total, 'page': params['page'], 'page_size': params['page_size'],
            'total_pages': pages, 'has_previous': params['page'] > 1, 'has_next': params['page'] < pages}


@transaction.atomic
def reward_vip_customers(params, admin):
    # All batches lock voucher first, then customers in ID order. The customer
    # lock serializes concurrent rewards using different voucher codes.
    try:
        Voucher.objects.select_for_update().get(pk=params['voucher_id'], distribution_type='ASSIGNED')
    except Voucher.DoesNotExist:
        raise serializers.ValidationError({'voucher_id': 'Chọn voucher cấp riêng hợp lệ.'})
    ids = params['customer_ids']
    locked_ids = list(get_user_model().objects.select_for_update().filter(
        pk__in=ids, role='CUSTOMER', is_active=True).order_by('pk').values_list('pk', flat=True))
    eligible = set(eligible_customers(params).filter(customer_id__in=ids).values_list('customer_id', flat=True))
    if set(locked_ids) != set(ids) or eligible != set(ids):
        raise serializers.ValidationError({'customer_ids': 'Có khách không còn đạt tiêu chí VIP. Vui lòng tải lại danh sách.'})
    month = report_month(params)
    existing = set(MonthlyVipReward.objects.filter(month=month, customer_id__in=ids).values_list('customer_id', flat=True))
    assigned = []
    for customer_id in ids:
        if customer_id in existing:
            continue
        user_voucher = assign_voucher_to_customer(
            voucher_id=params['voucher_id'], customer_id=customer_id, admin_user=admin,
            note=f'Tri ân VIP tháng {month:%m/%Y}',
        )
        MonthlyVipReward.objects.create(customer_id=customer_id, month=month, user_voucher=user_voucher)
        assigned.append(customer_id)
    return {'assigned_customer_ids': assigned, 'skipped_customer_ids': sorted(existing),
            'assigned_count': len(assigned), 'skipped_count': len(existing)}
