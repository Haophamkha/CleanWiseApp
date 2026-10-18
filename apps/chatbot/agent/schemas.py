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


class PrepareBookingInput(BaseModel):
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


@dataclass(frozen=True)
class AgentContext:
    customer_id: int
    deadline: float
    conversation_id: int = 0
    run_id: str = ''
    user_text: str = ''


@dataclass
class AgentAnswer:
    text: str
    card_references: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
