from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchServicesInput(BaseModel):
    # ToolNode adds a trusted runtime before validation; unknown model arguments
    # are discarded, while runtime is injected independently by LangChain.
    model_config = ConfigDict(extra='ignore')
    query: str = Field(default='', max_length=150)
    section_code: str = Field(default='', max_length=150)
    page: int = Field(default=1, ge=1, le=10000)


class ServiceDetailsInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    service_id: int = Field(gt=0)


class ListBookingsInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    # Gemini rejects empty strings inside enum values. Omitted/null means no
    # status filter; the enum only contains actual booking statuses.
    status: Literal['PENDING', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'FAILED'] | None = Field(
        default=None, description='Lọc trạng thái đơn; bỏ qua hoặc null để xem mọi trạng thái.')
    date_from: str = Field(default='', max_length=10, description='Ngày lịch dịch vụ từ YYYY-MM-DD, không phải ngày tạo đơn.')
    date_to: str = Field(default='', max_length=10, description='Ngày lịch dịch vụ đến YYYY-MM-DD, bao gồm ngày cuối.')
    page: int = Field(default=1, ge=1, le=10000)
    payment_status: Literal['UNPAID', 'PAID', 'REFUNDED'] | None = Field(
        default=None, description='Lọc thanh toán: chưa thanh toán, đã thanh toán, đã hoàn tiền.')
    service_query: str = Field(default='', max_length=150, description='Tên hoặc mã dịch vụ, ví dụ dọn nhà, máy lạnh.')
    limit: int = Field(default=5, ge=1, le=5, description='Số đơn mỗi trang. Hỏi đơn gần nhất dùng 1; hỏi N đơn dùng N (tối đa 5).')


class ResponseCardReference(BaseModel):
    type: Literal['service', 'booking', 'schedule', 'help']
    id: int = Field(gt=0)


class SelectResponseCardsInput(BaseModel):
    references: list[ResponseCardReference] = Field(
        max_length=5, description='Chỉ chọn ID đã có trong kết quả công cụ lượt này và phù hợp câu trả lời; [] nếu không cần thẻ.')


class BookingDetailsInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    booking_reference: str = Field(min_length=1, max_length=30, description='Mã đơn hoặc ID lấy từ list_my_bookings.')


class SearchHelpInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    query: str = Field(min_length=2, max_length=250, description='Từ khóa chủ đề đầy đủ, dựa vào câu hỏi tiếp nối; ví dụ nạp tiền vào ví, đặt đơn định kỳ.')


class PrepareBookingDraftInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    service_id: int = Field(gt=0)
    address_id: int = Field(gt=0)
    delivery_address_id: int | None = Field(default=None, gt=0)
    preferred_worker_id: int | None = Field(default=None, gt=0)
    service_data: dict = Field(description='Giá trị form_schema.fields khách đã cung cấp; không tự điền trường còn thiếu.')
    payment_method: Literal['CASH', 'BANK_TRANSFER', 'WALLET']
    voucher_code: str = Field(default='', max_length=80)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='ignore')


class RuntimeOnlyInput(BaseModel):
    """A harmless field lets LangChain inject ToolRuntime for zero-data tools."""
    model_config = ConfigDict(extra='ignore')
    acknowledge: bool = Field(default=True, description='Không cần cung cấp thông tin bổ sung.')


class BookingSchedulesInput(BookingDetailsInput):
    page: int = Field(default=1, ge=1, le=10000)


class PrepareBookingInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    service_id: int = Field(gt=0)
    service_data_json: str = Field(
        min_length=2, max_length=4000,
        description='Chuỗi JSON object service_data đúng key/value trong form_schema của dịch vụ. '
                    'Ngày YYYY-MM-DD, giờ HH:MM. Ví dụ: {"date":"2026-10-20","start_time":"09:00","duration":"3_HOURS"}')
    address_id: int = Field(gt=0, description='ID địa chỉ từ list_my_addresses')
    payment_method: Literal['CASH', 'BANK_TRANSFER', 'WALLET']
    delivery_address_id: int | None = Field(default=None, description='Chỉ cho dịch vụ có address_count=2 (chuyển nhà)')
    note: str = Field(default='', max_length=500)
    voucher_code: str = Field(default='', max_length=50,
                              description='Mã voucher khách chọn từ list_my_vouchers; bỏ trống nếu không dùng')
    preferred_worker_id: int | None = Field(default=None,
                                            description='ID nhân viên yêu thích từ list_favorite_workers; bỏ trống nếu không chọn')


class ListFavoriteWorkersInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    service_id: int = Field(gt=0)
    address_id: int = Field(gt=0, description='ID địa chỉ khách đã chọn')


class PrepareCancelInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    booking_reference: str = Field(min_length=1, max_length=30, description='Mã đơn hoặc ID')
    schedule_no: int | None = Field(default=None, ge=1,
                                    description='Số thứ tự buổi nếu chỉ hủy MỘT buổi; bỏ trống để hủy cả đơn')
    reason: str = Field(min_length=3, max_length=300, description='Lý do khách nêu')


class WalletHistoryInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    page: int = Field(default=1, ge=1, le=10000)

class ConfirmBookingInput(BaseModel):
    model_config = ConfigDict(extra='ignore')


@dataclass(frozen=True)
class AgentContext:
    customer_id: int | None
    deadline: float
    conversation_id: int = 0
    run_id: str = ''
    user_text: str = ''
    thread_id: str = ''
    worker_id: int | None = None
    role: str = 'CUSTOMER'


@dataclass
class AgentAnswer:
    text: str
    card_references: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class SearchWardsInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    ward_name: str = Field(min_length=1, max_length=100, description='Tên phường/xã khách nói, ví dụ "Bến Nghé"')
    city: str = Field(default='', max_length=100,
                      description='Tỉnh/thành phố khách nói (nếu có), ví dụ "TP.HCM"; bỏ trống nếu khách chưa nói')


class PrepareAddressInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    receiver_name: str = Field(min_length=1, max_length=150, description='Tên người nhận khách nói')
    receiver_phone: str = Field(min_length=9, max_length=15, description='Số điện thoại khách nói')
    address_line: str = Field(min_length=3, max_length=300, description='Số nhà, tên đường khách nói')
    ward_code: str = Field(min_length=1, max_length=10, description='ward_code lấy từ search_wards trong lượt này')
    label: str = Field(default='', max_length=100, description='Tên gợi nhớ (Nhà, Công ty...); bỏ trống nếu khách không nói')




class WorkerScheduleInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    status: Literal['PENDING', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'MISSED'] | None = Field(
        default=None, description='Lọc trạng thái buổi; MISSED = buổi bị hệ thống tự hủy do không check-in. Bỏ qua để xem các buổi sắp tới (chờ làm, đang làm).')
    date_from: str = Field(default='', max_length=10, description='Từ ngày YYYY-MM-DD.')
    date_to: str = Field(default='', max_length=10, description='Đến ngày YYYY-MM-DD (bao gồm). Hỏi 1 ngày thì để bằng date_from.')
    page: int = Field(default=1, ge=1, le=10000)


class EarningsInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    period: Literal['today', 'week', 'month'] = Field(default='week', description='Hôm nay, tuần này hoặc tháng này.')


class EarningHistoryInput(EarningsInput):
    page: int = Field(default=1, ge=1, le=10000)


class InvitationResponseInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    invitation_id: int = Field(gt=0, description='invitation_id lấy từ list_pending_invitations trong lượt này.')
    action: Literal['ACCEPT', 'REJECT'] = Field(description='ACCEPT = nhận việc, REJECT = từ chối.')


class PrepareComplaintInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    booking_reference: str = Field(min_length=1, max_length=30, description='Mã đơn (CW-...).')
    schedule_no: int | None = Field(default=None, ge=1, description='Số thứ tự buổi; bỏ trống nếu đơn chỉ có 1 buổi của nhân viên.')
    issue_type_id: int = Field(gt=0, description='id từ list_complaint_issue_types trong lượt này.')
    content: str = Field(min_length=5, max_length=1000, description='Nội dung nhân viên tự mô tả.')


class PreferredResponseInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    booking_reference: str = Field(min_length=1, max_length=30, description='Mã đơn (CW-...) lấy từ customer_requests của list_pending_invitations.')
    action: Literal['ACCEPT', 'REJECT'] = Field(description='ACCEPT = nhận, REJECT = từ chối.')


class CheckOutInput(BaseModel):
    model_config = ConfigDict(extra='ignore')
    booking_reference: str = Field(min_length=1, max_length=30, description='Mã đơn (CW-...).')
    schedule_no: int | None = Field(default=None, ge=1, description='Số thứ tự buổi; bỏ trống nếu chỉ có 1 buổi đang làm.')
    completion_note: str = Field(default='', max_length=500, description='Ghi chú hoàn thành do nhân viên tự nói; để trống nếu không có.')
