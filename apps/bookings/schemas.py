from drf_spectacular.utils import extend_schema, extend_schema_view

BOOKING_CUSTOMER_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='customer_booking_list',
        summary='Danh sách đơn dịch vụ của khách hàng',
        description='API dùng để lấy danh sách toàn bộ các đơn dịch vụ/booking mà khách hàng đang đăng nhập đã đặt. Có hỗ trợ phân trang và lọc theo trạng thái (status).',
        tags=['Booking - Customer']
    ),
    post=extend_schema(
        operation_id='customer_booking_create',
        summary='Tạo booking mới',
        description='API cho phép khách hàng tạo một đơn đặt lịch dịch vụ mới, bao gồm chọn dịch vụ, địa chỉ, lịch trình (schedules), dữ liệu dịch vụ tùy chỉnh và áp dụng mã voucher (nếu có).',
        tags=['Booking - Customer']
    ),
)


BOOKING_DETAIL_CUSTOMER_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='customer_booking_detail',
        summary='Chi tiết đơn dịch vụ của khách hàng',
        description='API dùng để xem thông tin chi tiết của một đơn dịch vụ cụ thể dựa vào ID (pk), bao gồm cấu hình dịch vụ, danh sách lịch trình, bảng giá chi tiết và thông tin thanh toán.',
        tags=['Booking - Customer']
    ),
)