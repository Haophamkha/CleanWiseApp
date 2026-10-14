from apps.bookings.models import Booking, BookingSchedule
from apps.services.models import Service

from .queries import booking_summary, local_time
from .knowledge import published_articles


def build_cards(customer_id, references, role='CUSTOMER'):
    """Only tool artifacts can propose references. Recheck ownership/live state."""
    cards, seen = [], set()
    for reference in references:
        if not isinstance(reference, dict):
            continue
        kind, object_id = reference.get('type'), reference.get('id')

        # Thẻ không gắn với bản ghi nào (nút mở màn chọn địa chỉ trên bản đồ).
        if kind == 'address_map':
            if ('address_map', 0) not in seen:
                seen.add(('address_map', 0))
                cards.append({'type': 'address_map', 'title': 'Thêm địa chỉ trên bản đồ',
                              'action': 'OPEN_ADDRESS_MAP', 'label': 'Chọn trên bản đồ'})
            continue

        if kind not in ('service', 'booking', 'schedule', 'help') or type(object_id) is not int or object_id <= 0:
            continue
        key = (kind, object_id)
        if key in seen:
            continue
        seen.add(key)
        if kind == 'service':
            obj = Service.objects.filter(pk=object_id, is_active=True).first()
            if obj:
                cards.append({'type': kind, 'service_id': obj.id, 'title': obj.name,
                              'action': 'OPEN_SERVICE', 'label': 'Xem dịch vụ'})
        elif kind == 'booking':
            obj = Booking.objects.filter(pk=object_id, customer_id=customer_id).select_related('service').first()
            if obj:
                cards.append({'type': kind, 'booking_id': obj.id, 'title': obj.booking_code,
                              'data': booking_summary(obj), 'action': 'OPEN_BOOKING', 'label': 'Xem đơn'})
        elif kind == 'help':
            obj = published_articles(role).filter(pk=object_id).first()
            if obj:
                cards.append({'type': kind, 'article_id': obj.pk, 'title': obj.title,
                              'version': obj.version, 'effective_from': obj.effective_from.isoformat(),
                              'action': 'OPEN_HELP_ARTICLE', 'label': 'Xem nguồn hướng dẫn'})
        else:
            obj = BookingSchedule.objects.filter(pk=object_id, booking__customer_id=customer_id).first()
            if obj:
                cards.append({'type': kind, 'schedule_id': obj.id, 'booking_id': obj.booking_id,
                              'title': f'Buổi {obj.sequence_no}', 'scheduled_start': local_time(obj.scheduled_start),
                              'status': obj.status, 'status_label': obj.get_status_display(),
                              'action': 'OPEN_SCHEDULE', 'label': 'Xem buổi dịch vụ'})
        if len(cards) == 5:
            break
    return cards