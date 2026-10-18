import json
import time

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections
from django.http import Http404
from langchain.tools import ToolRuntime, tool
from rest_framework import serializers

from .schemas import (AgentContext, BookingDetailsInput, BookingSchedulesInput,
                      ListBookingsInput, PrepareBookingInput, RuntimeOnlyInput,
                      SearchServicesInput, ServiceDetailsInput, SelectResponseCardsInput, SearchHelpInput)
from ..services import queries
from ..services.booking import (confirm_booking as create_confirmed_booking,
                                list_customer_addresses, prepare_booking,
                                discard_booking_draft)
from ..services.knowledge import search_help


EXPLICIT_BOOKING_CONFIRMATIONS = {
    'ok', 'okay', 'đồng ý', 'xác nhận', 'xác nhận đặt', 'xác nhận đặt lịch',
    'tôi xác nhận', 'đồng ý đặt', 'đồng ý đặt lịch', 'tôi đồng ý đặt lịch',
}


def is_explicit_booking_confirmation(text):
    normalized = ' '.join(text.strip().lower().rstrip('.!?').split())
    return normalized in EXPLICIT_BOOKING_CONFIRMATIONS


def result(callback, runtime):
    if time.monotonic() > runtime.context.deadline:
        raise TimeoutError('Chatbot run deadline exceeded')
    close_old_connections()
    try:
        data, cards = callback()
        return json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False), {'cards': cards}
    except queries.ToolQueryError as exc:
        return json.dumps({'error': 'NOT_FOUND_OR_INVALID_INPUT', 'detail': str(exc)}, ensure_ascii=False), {'cards': []}
    except serializers.ValidationError as exc:
        return json.dumps({'error': 'INVALID_BOOKING_INPUT', 'detail': str(exc.detail)},
                          cls=DjangoJSONEncoder, ensure_ascii=False), {'cards': []}
    except (Http404, ObjectDoesNotExist):
        # A stale or invalid service/address ID is a recoverable tool result,
        # not a reason to fail the entire chatbot turn with a 503.
        return json.dumps({'error': 'NOT_FOUND_OR_INVALID_INPUT',
                           'detail': 'Không tìm thấy dịch vụ hoặc địa chỉ hợp lệ. Hãy hỏi khách chọn lại.'},
                          ensure_ascii=False), {'cards': []}
    except DjangoValidationError as exc:
        return json.dumps({'error': 'INVALID_BOOKING_INPUT', 'detail': exc.messages},
                          cls=DjangoJSONEncoder, ensure_ascii=False), {'cards': []}
    finally:
        close_old_connections()


@tool(args_schema=SearchServicesInput, response_format='content_and_artifact')
def search_services(runtime: ToolRuntime[AgentContext], query: str = '', section_code: str = '', page: int = 1):
    """Tìm dịch vụ đang hoạt động. Dùng từ khóa ngắn (ví dụ dọn nhà, máy lạnh), hoặc query rỗng để xem danh mục."""
    def fetch():
        data = queries.search_services(query, section_code, page)
        return data, [{'type': 'service', 'id': item['id']} for item in data['results']]
    return result(fetch, runtime)


@tool(args_schema=ServiceDetailsInput, response_format='content_and_artifact')
def get_service_details(service_id: int, runtime: ToolRuntime[AgentContext]):
    """Xem mô tả, lựa chọn form và bảng giá một dịch vụ. Bảng giá chưa phải tổng tiền sau voucher/số buổi."""
    return result(lambda: (queries.get_service_details(service_id), [{'type': 'service', 'id': service_id}]), runtime)


@tool(args_schema=ListBookingsInput, response_format='content_and_artifact')
def list_my_bookings(runtime: ToolRuntime[AgentContext], status: str | None = None, date_from: str = '', date_to: str = '', page: int = 1,
                     payment_status: str | None = None, service_query: str = '', limit: int = 5):
    """Tra đơn phù hợp trạng thái, thanh toán, dịch vụ và ngày lịch dịch vụ. Mới nhất trước, limit=1 cho đơn gần nhất."""
    def fetch():
        data = queries.list_my_bookings(runtime.context.customer_id, status, date_from, date_to, page,
                                       payment_status, service_query, limit)
        return data, [{'type': 'booking', 'id': item['id']} for item in data['results']]
    return result(fetch, runtime)


@tool(args_schema=BookingDetailsInput, response_format='content_and_artifact')
def get_my_booking_detail(booking_reference: str, runtime: ToolRuntime[AgentContext]):
    """Xem đơn của khách: tiến độ, thanh toán, số buổi còn lại, lịch tiếp theo và check-in/phân công thực tế."""
    def fetch():
        data = queries.get_my_booking_detail(runtime.context.customer_id, booking_reference)
        return data, [{'type': 'booking', 'id': data['id']}]
    return result(fetch, runtime)


@tool(args_schema=BookingSchedulesInput, response_format='content_and_artifact')
def get_my_booking_schedules(booking_reference: str, runtime: ToolRuntime[AgentContext], page: int = 1):
    """Xem các buổi trong đơn của khách, 10 buổi/trang. Dùng khi đơn có nhiều buổi hoặc cần xem buổi cụ thể."""
    def fetch():
        data = queries.get_my_booking_schedules(runtime.context.customer_id, booking_reference, page)
        return data, [{'type': 'schedule', 'id': item['id']} for item in data['results']]
    return result(fetch, runtime)


@tool(args_schema=SelectResponseCardsInput, response_format='content_and_artifact')
def select_response_cards(references: list):
    """Chọn thẻ liên quan để gửi kèm câu trả lời sau khi tra cứu. Không gửi mọi đơn đã xem để tìm kiếm. Chọn [] khi không có kết quả phù hợp."""
    selected = [ref.model_dump() if hasattr(ref, 'model_dump') else ref for ref in references]
    return 'Đã ghi nhận lựa chọn thẻ. Hãy trả lời khách bằng lời.', {'selected_cards': selected}


@tool(args_schema=SearchHelpInput, response_format='content_and_artifact')
def search_help_articles(query: str, runtime: ToolRuntime[AgentContext]):
    """Tìm hướng dẫn/FAQ/chính sách đã xuất bản cho khách hàng. Trả đoạn nguồn và phiên bản; không dùng cho giá/số dư/trạng thái cá nhân hiện tại."""
    return result(lambda: search_help(query), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def list_my_addresses(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Liệt kê địa chỉ đã lưu của khách đang đăng nhập để khách chọn khi đặt lịch. Không trả số điện thoại."""
    return result(lambda: (list_customer_addresses(runtime.context.customer_id), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def get_my_wallet_balance(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Đọc số dư ví của chính khách đang đăng nhập. Chỉ đọc; không nạp, rút hoặc chuyển tiền."""
    from apps.wallets.models import Wallet
    def fetch():
        wallet = Wallet.objects.filter(user_id=runtime.context.customer_id).first()
        return ({'balance': str(wallet.balance), 'currency': 'VND'} if wallet else
                {'balance': '0.00', 'currency': 'VND'}), []
    return result(fetch, runtime)


@tool(args_schema=PrepareBookingInput, response_format='content_and_artifact')
def prepare_booking_draft(runtime: ToolRuntime[AgentContext], service_id: int, address_id: int,
                          service_data: dict, payment_method: str, delivery_address_id: int | None = None,
                          preferred_worker_id: int | None = None, voucher_code: str = ''):
    """Kiểm tra dữ liệu và báo giá đặt lịch, lưu bản nháp 10 phút. KHÔNG tạo đơn. Sau đó phải hỏi xác nhận."""
    def fetch():
        quote = prepare_booking(
            customer_id=runtime.context.customer_id,
            conversation_id=runtime.context.conversation_id,
            run_id=runtime.context.run_id,
            service_id=service_id, address_id=address_id, delivery_address_id=delivery_address_id,
            service_data=service_data, payment_method=payment_method,
            preferred_worker_id=preferred_worker_id, voucher_code=voucher_code,
        )
        return quote, []
    return result(fetch, runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def confirm_booking_draft(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Tạo đơn từ bản nháp chỉ khi tin nhắn hiện tại là xác nhận rõ ràng và bản nháp được hỏi ở lượt chat trước."""
    if not is_explicit_booking_confirmation(runtime.context.user_text):
        return json.dumps({'error': 'EXPLICIT_CONFIRMATION_REQUIRED'}, ensure_ascii=False), {'cards': []}
    def fetch():
        booking = create_confirmed_booking(
            customer_id=runtime.context.customer_id,
            conversation_id=runtime.context.conversation_id,
            run_id=runtime.context.run_id,
        )
        return {'id': booking.id, 'booking_code': booking.booking_code,
                'service_name': booking.service.name, 'total_amount': str(booking.total_amount),
                'payment_method': booking.payments.order_by('-created_at').values_list('method', flat=True).first()}, [
                    {'type': 'booking', 'id': booking.id}]
    return result(fetch, runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def discard_booking_draft_tool(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Hủy bản nháp chưa xác nhận khi khách từ chối hoặc đổi ý. Không hủy đơn đã tạo."""
    def fetch():
        discard_booking_draft(customer_id=runtime.context.customer_id,
                              conversation_id=runtime.context.conversation_id)
        return {'discarded': True}, []
    return result(fetch, runtime)


CUSTOMER_TOOLS = [search_services, get_service_details, list_my_bookings, search_help_articles,
                  get_my_booking_detail, get_my_booking_schedules, select_response_cards,
                  list_my_addresses, get_my_wallet_balance, prepare_booking_draft,
                  confirm_booking_draft, discard_booking_draft_tool]
