import json
import re  # MỚI
import time

from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage  # MỚI

from .schemas import (AgentContext, BookingDetailsInput, BookingSchedulesInput, ConfirmBookingInput,
                      ListBookingsInput, PrepareBookingInput, SearchServicesInput, ServiceDetailsInput,
                      SelectResponseCardsInput, SearchHelpInput, ListFavoriteWorkersInput,
                      PrepareCancelInput, WalletHistoryInput,SearchWardsInput, PrepareAddressInput)
from ..services import queries, booking_tool,address_tool
from ..services.knowledge import search_help


def result(callback, runtime):
    if time.monotonic() > runtime.context.deadline:
        raise TimeoutError('Chatbot run deadline exceeded')
    close_old_connections()
    try:
        data, cards = callback()
        return json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False), {'cards': cards}
    except queries.ToolQueryError as exc:
        return json.dumps({'error': 'NOT_FOUND_OR_INVALID_INPUT', 'detail': str(exc)}, ensure_ascii=False), {'cards': []}
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


@tool(response_format='content_and_artifact')
def list_my_addresses(runtime: ToolRuntime[AgentContext]):
    """Liệt kê địa chỉ đã lưu của khách (id, nhãn, địa chỉ, mặc định). Dùng trước khi đặt lịch."""
    return result(lambda: (booking_tool.list_my_addresses(runtime.context.customer_id), []), runtime)


# MỚI: chốt chặn, khách phải tự nhắc tới phương thức thanh toán thì mới được tạo bản nháp.
_PAYMENT_PATTERNS = {
    'CASH': r'tiền mặt|tien mat|\bcash\b',
    'BANK_TRANSFER': r'chuyển khoản|chuyen khoan|ngân hàng|ngan hang|\bbank\b|\bck\b',
    'WALLET': r'\bví\b(?!\s+dụ)|\bwallet\b',
}


def _customer_mentioned_payment(runtime, payment_method):
    pattern = _PAYMENT_PATTERNS.get(payment_method)
    if not pattern:
        return False
    for message in runtime.state.get('messages', []):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            if re.search(pattern, message.content.casefold()):
                return True
    return False


@tool(args_schema=PrepareBookingInput, response_format='content_and_artifact')
def prepare_booking(service_id: int, service_data_json: str, address_id: int, payment_method: str,
                    runtime: ToolRuntime[AgentContext], delivery_address_id: int | None = None, note: str = '',
                    voucher_code: str = '', preferred_worker_id: int | None = None):
    """Kiểm tra thông tin, báo giá (kèm voucher, nhân viên yêu thích nếu có) và tạo BẢN NHÁP đặt lịch. KHÔNG tạo đơn. Chỉ gọi khi khách đã nói đủ thông tin."""
    def fetch():
        if not _customer_mentioned_payment(runtime, payment_method):
            raise queries.ToolQueryError(
                'Khách chưa tự nói phương thức thanh toán này. Chỉ hỏi khách chọn tiền mặt, chuyển khoản '
                'hay ví, rồi DỪNG chờ trả lời. Không tự chọn mặc định.')
        try:
            service_data = json.loads(service_data_json)
        except ValueError as exc:
            raise queries.ToolQueryError('service_data_json không phải JSON hợp lệ.') from exc
        if not isinstance(service_data, dict):
            raise queries.ToolQueryError('service_data_json phải là JSON object.')
        data = booking_tool.prepare_booking(runtime.context.customer_id, runtime.context.thread_id, service_id,
                                            service_data, address_id, delivery_address_id, payment_method, note,
                                            voucher_code, preferred_worker_id)
        return data, []
    return result(fetch, runtime)

@tool(response_format='content_and_artifact')
def confirm_booking(runtime: ToolRuntime[AgentContext]):
    """Tạo đơn thật từ bản nháp gần nhất. CHỈ gọi sau khi khách đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    def fetch():
        data = booking_tool.confirm_booking(runtime.context.customer_id, runtime.context.thread_id)
        return data, [{'type': 'booking', 'id': data['id']}]
    return result(fetch, runtime)



@tool(response_format='content_and_artifact')
def list_my_vouchers(runtime: ToolRuntime[AgentContext]):
    """Liệt kê voucher còn dùng được trong ví của khách (mã, mức giảm, đơn tối thiểu, hạn). Chỉ gọi để hỏi khách có muốn dùng voucher khi đặt lịch."""
    return result(lambda: (booking_tool.list_my_vouchers(runtime.context.customer_id), []), runtime)


@tool(args_schema=ListFavoriteWorkersInput, response_format='content_and_artifact')
def list_favorite_workers(service_id: int, address_id: int, runtime: ToolRuntime[AgentContext]):
    """Liệt kê nhân viên yêu thích của khách phù hợp dịch vụ và khu vực địa chỉ đã chọn (id, tên). Dùng khi đặt lịch."""
    return result(lambda: (booking_tool.list_favorite_workers(
        runtime.context.customer_id, service_id, address_id), []), runtime)


@tool(args_schema=WalletHistoryInput, response_format='content_and_artifact')
def get_my_wallet(runtime: ToolRuntime[AgentContext], page: int = 1):
    """Xem số dư ví CleanWise và lịch sử giao dịch gần nhất của khách (5 giao dịch/trang)."""
    return result(lambda: (booking_tool.get_wallet(runtime.context.customer_id, page), []), runtime)


@tool(args_schema=BookingDetailsInput, response_format='content_and_artifact')
def get_payment_link(booking_reference: str, runtime: ToolRuntime[AgentContext]):
    """Lấy link và thông tin chuyển khoản cho đơn CHUYỂN KHOẢN đang chờ thanh toán."""
    def fetch():
        data = booking_tool.get_payment_link(runtime.context.customer_id, booking_reference)
        return data, [{'type': 'booking', 'id': data['booking_id']}]
    return result(fetch, runtime)


@tool(args_schema=BookingDetailsInput, response_format='content_and_artifact')
def get_booking_for_reorder(booking_reference: str, runtime: ToolRuntime[AgentContext]):
    """Lấy lựa chọn của một đơn cũ (dịch vụ, service_data, địa chỉ) để đặt lại. Phải hỏi khách ngày giờ mới."""
    def fetch():
        data = booking_tool.get_reorder_template(runtime.context.customer_id, booking_reference)
        return data, [{'type': 'service', 'id': data['service_id']}]
    return result(fetch, runtime)


@tool(args_schema=PrepareCancelInput, response_format='content_and_artifact')
def prepare_cancel(booking_reference: str, reason: str, runtime: ToolRuntime[AgentContext],
                   schedule_no: int | None = None):
    """Kiểm tra điều kiện, tính hoàn tiền dự kiến và tạo BẢN NHÁP hủy đơn/hủy một buổi. KHÔNG hủy. Chỉ gọi khi khách đã nói rõ đơn nào, buổi nào (nếu có) và lý do."""
    def fetch():
        data = booking_tool.prepare_cancel(runtime.context.customer_id, runtime.context.thread_id,
                                           booking_reference, schedule_no, reason)
        return data, []
    return result(fetch, runtime)


@tool(response_format='content_and_artifact')
def confirm_cancel(runtime: ToolRuntime[AgentContext]):
    """Thực hiện hủy từ bản nháp gần nhất. CHỈ gọi sau khi khách đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    def fetch():
        data = booking_tool.confirm_cancel(runtime.context.customer_id, runtime.context.thread_id)
        return data, [{'type': 'booking', 'id': data['id']}]
    return result(fetch, runtime)


@tool(args_schema=SearchWardsInput, response_format='content_and_artifact')
def search_wards(ward_name: str, runtime: ToolRuntime[AgentContext], city: str = ''):
    """Tra phường/xã trong hệ thống để lấy ward_code khi thêm địa chỉ mới. Trả tối đa 10 kết quả."""
    return result(lambda: (address_tool.search_wards(ward_name, city), []), runtime)


@tool(args_schema=PrepareAddressInput, response_format='content_and_artifact')
def prepare_address(receiver_name: str, receiver_phone: str, address_line: str, ward_code: str,
                    runtime: ToolRuntime[AgentContext], label: str = ''):
    """Kiểm tra và tạo BẢN NHÁP địa chỉ mới. KHÔNG lưu. Chỉ gọi khi khách đã tự nói đủ người nhận, số điện thoại, số nhà/đường và đã chọn phường/xã."""
    def fetch():
        data = address_tool.prepare_address(runtime.context.customer_id, runtime.context.thread_id,
                                            receiver_name, receiver_phone, address_line, ward_code, label)
        return data, []
    return result(fetch, runtime)


@tool(response_format='content_and_artifact')
def confirm_address(runtime: ToolRuntime[AgentContext]):
    """Lưu địa chỉ từ bản nháp gần nhất. CHỈ gọi sau khi khách đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    def fetch():
        data = address_tool.confirm_address(runtime.context.customer_id, runtime.context.thread_id)
        return data, []
    return result(fetch, runtime)



@tool(response_format='content_and_artifact')
def open_address_map_picker(runtime: ToolRuntime[AgentContext]):
    """Hiện nút mở màn chọn địa chỉ trên bản đồ trong ứng dụng. Dùng khi khách muốn thêm địa chỉ mới hoặc muốn chọn vị trí trên bản đồ."""
    data = {'ok': True, 'note': 'Đã hiện nút chọn trên bản đồ. Bảo khách bấm nút, lưu địa chỉ xong sẽ quay lại và báo cho bạn.'}
    return json.dumps(data, ensure_ascii=False), {'cards': [{'type': 'address_map'}]}

CUSTOMER_TOOLS = [search_services, get_service_details, list_my_bookings, search_help_articles,
                  get_my_booking_detail, get_my_booking_schedules, select_response_cards,
                  list_my_addresses, prepare_booking, confirm_booking,
                  list_my_vouchers, list_favorite_workers, get_my_wallet, get_payment_link,
                  get_booking_for_reorder, prepare_cancel, confirm_cancel, search_wards, prepare_address, confirm_address, open_address_map_picker]