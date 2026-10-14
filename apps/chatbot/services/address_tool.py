import re
import unicodedata
from types import SimpleNamespace

from django.core.cache import cache
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.addresses.serializers import CustomerAddressSerializer
from apps.worker.models import Area

from .booking_tool import DRAFT_TTL, _conversation_id, _flatten, _full_address, _get_customer
from .queries import ToolQueryError

MAX_ADDRESSES = 10
MAX_WARD_RESULTS = 10
AREAS_CACHE_KEY = 'chatbot:areas:v1'

_WARD_PREFIX = re.compile(r'^(phuong|xa|thi tran|dac khu)\s+|^p\.\s*')
_CITY_PREFIX = re.compile(r'^(thanh pho|tp|tinh)\s+')
_CITY_ALIASES = {
    'hcm': 'ho chi minh', 'tphcm': 'ho chi minh', 'sai gon': 'ho chi minh', 'saigon': 'ho chi minh',
    'hn': 'ha noi',
}


def _plain(text):
    text = unicodedata.normalize('NFD', (text or '').replace('đ', 'd').replace('Đ', 'D'))
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text.lower().replace('.', ' ')).strip()


def _norm_ward(text):
    return _WARD_PREFIX.sub('', _plain(text)).strip()


def _norm_city(text):
    value = _CITY_PREFIX.sub('', _plain(text)).strip()
    return _CITY_ALIASES.get(value, value)


def _address_key(customer_id, thread_id):
    return f'chatbot:address-draft:{customer_id}:{_conversation_id(thread_id)}'


def _load_areas():
    areas = cache.get(AREAS_CACHE_KEY)
    if areas is None:
        areas = [
            {'ward_code': code, 'name': name, 'city': city,
             'n_name': _norm_ward(name), 'n_city': _norm_city(city)}
            for code, name, city in Area.objects.filter(is_active=True).values_list('ward_code', 'name', 'city')
        ]
        cache.set(AREAS_CACHE_KEY, areas, 3600)
    return areas


def search_wards(ward_name, city=''):
    query = _norm_ward(ward_name)
    if not query:
        raise ToolQueryError('Thiếu tên phường/xã.')
    city_key = _norm_city(city) if city else ''
    ranked = []
    for area in _load_areas():
        if city_key and city_key not in area['n_city']:
            continue
        if area['n_name'] == query:
            rank = 0
        elif area['n_name'].startswith(query):
            rank = 1
        elif query in area['n_name']:
            rank = 2
        else:
            continue
        ranked.append((rank, area['city'], area['name'], area))
    ranked.sort(key=lambda r: r[:3])
    results = [{'ward_code': a['ward_code'], 'ward': a['name'], 'city': a['city']}
               for *_, a in ranked[:MAX_WARD_RESULTS]]
    note = ('Nếu có nhiều kết quả thì liệt kê để khách chọn, tuyệt đối không tự đoán. '
            'Nếu rỗng: hỏi lại khách tên phường/xã và tỉnh/thành theo đơn vị hành chính hiện hành, '
            'hoặc đề nghị thêm địa chỉ trong ứng dụng.')
    return {'results': results, 'count': len(ranked), 'has_more': len(ranked) > MAX_WARD_RESULTS, 'note': note}


def _serializer(customer, data):
    request = SimpleNamespace(user=customer)
    return CustomerAddressSerializer(data=data, context={'request': request})


def prepare_address(customer_id, thread_id, receiver_name, receiver_phone, address_line, ward_code, label=''):
    customer = _get_customer(customer_id)
    if CustomerAddress.objects.filter(customer_id=customer_id, is_active=True).count() >= MAX_ADDRESSES:
        raise ToolQueryError(f'Tài khoản đã đủ {MAX_ADDRESSES} địa chỉ. Hãy xóa bớt địa chỉ trong ứng dụng.')
    data = {
        'label': (label or '').strip() or 'Địa chỉ mới',
        'receiver_name': receiver_name.strip(), 'receiver_phone': receiver_phone.strip(),
        'address_line': address_line.strip(), 'ward_code': ward_code.strip(),
        'is_default': False,
    }
    ser = _serializer(customer, data)
    if not ser.is_valid():
        raise ToolQueryError(_flatten(ser.errors))
    v = ser.validated_data
    is_first = not CustomerAddress.objects.filter(customer_id=customer_id, is_active=True).exists()

    cache.set(_address_key(customer_id, thread_id), {
        'customer_id': customer_id, 'thread_id': thread_id, 'data': data}, DRAFT_TTL)

    full = ', '.join(p for p in (v['address_line'], v['ward'], v['city']) if p)
    lines = [
        f'Tên gợi nhớ: {v["label"]}',
        f'Người nhận: {v["receiver_name"]}',
        f'Số điện thoại: {v["receiver_phone"]}',
        f'Địa chỉ: {full}',
        'Địa chỉ mặc định: có (địa chỉ đầu tiên của bạn)' if is_first else 'Địa chỉ mặc định: không đổi',
    ]
    return {'created_address': False, 'confirmation_text': '\n'.join(lines),
            'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA LƯU. Hỏi "Bạn xác nhận thêm địa chỉ này không?" rồi dừng; '
                    'chỉ gọi confirm_address ở tin nhắn sau khi khách đồng ý.'}


def confirm_address(customer_id, thread_id):
    key = _address_key(customer_id, thread_id)
    draft = cache.get(key)
    if not draft or draft['customer_id'] != customer_id:
        raise ToolQueryError('Không có bản nháp địa chỉ đang chờ hoặc đã hết hạn. Hãy chuẩn bị lại.')
    if draft['thread_id'] == thread_id:
        raise ToolQueryError('Chưa có xác nhận của khách. Hãy tóm tắt, hỏi khách và chờ tin nhắn tiếp theo.')
    if not cache.delete(key):
        raise ToolQueryError('Bản nháp này đã được xử lý.')
    customer = _get_customer(customer_id)
    ser = _serializer(customer, draft['data'])
    if not ser.is_valid():
        raise ToolQueryError(_flatten(ser.errors))
    address = ser.save()
    return {'id': address.id, 'label': address.label, 'address': _full_address(address),
            'is_default': address.is_default,
            'next_step': 'Coi như khách đã chọn địa chỉ này. Nếu đang đặt lịch thì làm bước kế tiếp '
                         '(voucher/nhân viên hoặc thanh toán), dùng address_id này.'}