from langchain.tools import ToolRuntime, tool

from .schemas import (AgentContext, EarningHistoryInput, EarningsInput, InvitationResponseInput,
                      PrepareComplaintInput, PreferredResponseInput, SearchHelpInput, WalletHistoryInput,
                      WorkerScheduleInput, RuntimeOnlyInput)
from .tools import result
from ..services import queries, worker_tool
from ..services.knowledge import search_help

# Bật/tắt nhóm công cụ báo cáo sự cố (confirm_complaint đã nối với serializer khiếu nại thật).
WORKER_COMPLAINT_ENABLED = True


def _wid(runtime):
    ctx = runtime.context
    if ctx.role != 'WORKER' or not ctx.worker_id:
        raise queries.ToolQueryError('Công cụ này chỉ dành cho nhân viên.')
    return ctx.worker_id


@tool(args_schema=SearchHelpInput, response_format='content_and_artifact')
def search_worker_help(query: str, runtime: ToolRuntime[AgentContext]):
    """Tìm hướng dẫn sử dụng ứng dụng nhân viên, quy định, FAQ đã xuất bản dành cho nhân viên. Cũng dùng cho câu hỏi điều hướng "vào mục nào để làm X". Không dùng cho số liệu cá nhân."""
    return result(lambda: search_help(query, role='WORKER'), runtime)


@tool(args_schema=WorkerScheduleInput, response_format='content_and_artifact')
def get_my_schedule(runtime: ToolRuntime[AgentContext], status: str | None = None, date_from: str = '',
                    date_to: str = '', page: int = 1):
    """Xem lịch làm việc của nhân viên (các buổi đã nhận): giờ, dịch vụ, địa chỉ, tên khách, ghi chú, cách thanh toán. Mặc định là các buổi sắp tới, sớm nhất trước."""
    return result(lambda: (worker_tool.get_my_schedule(_wid(runtime), status, date_from, date_to, page), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def list_pending_invitations(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Liệt kê lời mời nhận việc đang chờ phản hồi (invitation_id, giờ, khu vực, hạn trả lời, thu nhập dự kiến) và yêu cầu đích danh từ khách. Gọi trước khi nhận/từ chối."""
    return result(lambda: (worker_tool.list_pending_invitations(_wid(runtime)), []), runtime)


@tool(args_schema=InvitationResponseInput, response_format='content_and_artifact')
def prepare_invitation_response(invitation_id: int, action: str, runtime: ToolRuntime[AgentContext]):
    """Kiểm tra và tạo BẢN NHÁP nhận hoặc từ chối một lời mời. KHÔNG phản hồi thật. Chỉ gọi khi nhân viên đã nói rõ lời mời nào và nhận hay từ chối."""
    return result(lambda: (worker_tool.prepare_invitation_response(
        _wid(runtime), runtime.context.thread_id, invitation_id, action), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def confirm_invitation_response(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Thực hiện nhận/từ chối lời mời từ bản nháp gần nhất. CHỈ gọi sau khi nhân viên đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    return result(lambda: (worker_tool.confirm_invitation_response(
        _wid(runtime), runtime.context.thread_id), []), runtime)


@tool(args_schema=PreferredResponseInput, response_format='content_and_artifact')
def prepare_preferred_response(booking_reference: str, action: str, runtime: ToolRuntime[AgentContext]):
    """Kiểm tra và tạo BẢN NHÁP nhận hoặc từ chối yêu cầu đích danh của khách (customer_requests). KHÔNG phản hồi thật. Chỉ gọi khi nhân viên đã nói rõ đơn nào và nhận hay từ chối."""
    return result(lambda: (worker_tool.prepare_preferred_response(
        _wid(runtime), runtime.context.thread_id, booking_reference, action), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def confirm_preferred_response(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Thực hiện nhận/từ chối yêu cầu đích danh từ bản nháp gần nhất. CHỈ gọi sau khi nhân viên đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    return result(lambda: (worker_tool.confirm_preferred_response(_wid(runtime), runtime.context.thread_id), []), runtime)


@tool(args_schema=EarningsInput, response_format='content_and_artifact')
def get_my_earnings(runtime: ToolRuntime[AgentContext], period: str = 'week'):
    """Tổng thu nhập hôm nay/tuần/tháng: số buổi, thu nhập sau hoa hồng, hoa hồng, khoản chờ vào ví, hoa hồng tiền mặt còn nợ."""
    return result(lambda: (worker_tool.get_my_earnings(_wid(runtime), period), []), runtime)


@tool(args_schema=EarningHistoryInput, response_format='content_and_artifact')
def get_earning_history(runtime: ToolRuntime[AgentContext], period: str = 'week', page: int = 1):
    """Chi tiết thu nhập từng buổi trong kỳ (5 buổi/trang)."""
    return result(lambda: (worker_tool.get_earning_history(_wid(runtime), period, page), []), runtime)


@tool(args_schema=WalletHistoryInput, response_format='content_and_artifact')
def get_worker_wallet(runtime: ToolRuntime[AgentContext], page: int = 1):
    """Số dư ví, số tiền rút được, hạn mức rút, lịch sử giao dịch và các lệnh rút gần đây của nhân viên. Số dư chỉ lấy từ công cụ này."""
    return result(lambda: (worker_tool.get_worker_wallet(_wid(runtime), page), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def get_my_reviews(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Điểm đánh giá trung bình, phân bố sao và 5 đánh giá gần nhất của nhân viên."""
    return result(lambda: (worker_tool.get_my_reviews(_wid(runtime)), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def get_my_profile_status(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Họ tên, trạng thái duyệt hồ sơ, lý do bị từ chối, mức hoàn thiện hồ sơ, khu vực làm việc, dịch vụ đã đăng ký, điểm đánh giá và số buổi đã hoàn thành. Khi nhân viên xem hồ sơ, nêu cả họ tên."""
    return result(lambda: (worker_tool.get_my_profile(_wid(runtime)), []), runtime)


@tool(args_schema=WalletHistoryInput, response_format='content_and_artifact')
def list_my_complaints(runtime: ToolRuntime[AgentContext], page: int = 1):
    """Danh sách và trạng thái các khiếu nại/báo cáo sự cố do chính nhân viên đã gửi."""
    return result(lambda: (worker_tool.list_my_complaints(_wid(runtime), page), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def list_complaint_issue_types(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Danh sách loại sự cố nhân viên được chọn khi báo cáo (khách vắng, địa chỉ sai...)."""
    return result(lambda: (worker_tool.list_complaint_issue_types(), []), runtime)


@tool(args_schema=PrepareComplaintInput, response_format='content_and_artifact')
def prepare_complaint(booking_reference: str, issue_type_id: int, content: str,
                      runtime: ToolRuntime[AgentContext], schedule_no: int | None = None):
    """Kiểm tra và tạo BẢN NHÁP báo cáo sự cố về một đơn. KHÔNG gửi. Chỉ gọi khi nhân viên đã nói rõ đơn nào, loại sự cố và nội dung."""
    return result(lambda: (worker_tool.prepare_complaint(
        _wid(runtime), runtime.context.thread_id, booking_reference, schedule_no, issue_type_id, content), []), runtime)


@tool(args_schema=RuntimeOnlyInput, response_format='content_and_artifact')
def confirm_complaint(runtime: ToolRuntime[AgentContext], acknowledge: bool = True):
    """Gửi báo cáo sự cố từ bản nháp gần nhất. CHỈ gọi sau khi nhân viên đã đồng ý rõ ràng ở tin nhắn mới nhất."""
    return result(lambda: (worker_tool.confirm_complaint(_wid(runtime), runtime.context.thread_id), []), runtime)


COMPLAINT_TOOLS = [list_complaint_issue_types, prepare_complaint, confirm_complaint]

WORKER_TOOLS = [search_worker_help, get_my_schedule, list_pending_invitations, prepare_invitation_response,
                confirm_invitation_response, prepare_preferred_response, confirm_preferred_response,
                get_my_earnings, get_earning_history, get_worker_wallet, get_my_reviews,
                get_my_profile_status, list_my_complaints,
                *(COMPLAINT_TOOLS if WORKER_COMPLAINT_ENABLED else [])]