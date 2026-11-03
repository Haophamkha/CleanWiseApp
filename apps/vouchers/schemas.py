from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view

from .serializers import (
    AdminAssignedUserVoucherSerializer,
    VoucherAdminAssignSerializer,
    VoucherAdminSerializer,
    VoucherAdminWriteSerializer,
)

# --- NHÓM VOUCHER - CUSTOMER ---

VOUCHER_CUSTOMER_LIST_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='customer_voucher_list',
        summary='Danh sách voucher công khai có thể nhận',
        description='Lấy danh sách các voucher ở trạng thái công khai (PUBLIC), còn hiệu lực và chưa hết lượt phát hành mà khách hàng chưa từng nhận.',
        tags=['Voucher - Customer']
    )
)

VOUCHER_CUSTOMER_DETAIL_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='customer_voucher_detail',
        summary='Chi tiết voucher công khai',
        description='Xem thông tin chi tiết của một voucher công khai dựa vào ID.',
        tags=['Voucher - Customer']
    )
)

VOUCHER_WALLET_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='customer_voucher_wallet_list',
        summary='Ví voucher của khách hàng',
        description='Lấy danh sách toàn bộ voucher đã lưu trữ trong ví cá nhân của khách hàng đang đăng nhập.',
        tags=['Voucher - Customer']
    )
)

VOUCHER_CLAIM_SCHEMA = extend_schema_view(
    post=extend_schema(
        operation_id='customer_voucher_claim',
        summary='Nhận voucher bằng mã code',
        description='Cho phép khách hàng nhập mã code để nhận và lưu voucher vào ví cá nhân.',
        tags=['Voucher - Customer']
    )
)

VOUCHER_VALIDATE_SCHEMA = extend_schema_view(
    post=extend_schema(
        operation_id='customer_voucher_validate',
        summary='Kiểm tra và tính toán giảm giá voucher',
        description='Xác thực mã voucher trong ví của khách hàng có hợp lệ với tổng tiền đơn dịch vụ (subtotal_amount) hiện tại hay không, đồng thời trả về số tiền được giảm.',
        tags=['Voucher - Customer']
    )
)


# --- NHÓM VOUCHER - ADMIN ---

VOUCHER_ADMIN_LIST_CREATE_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='admin_voucher_list',
        summary='Danh sách toàn bộ voucher (Admin)',
        description='Quản trị viên xem danh sách tất cả voucher trong hệ thống, hỗ trợ lọc theo trạng thái active, loại giảm giá, hình thức phát hành và tìm kiếm từ khóa.',
        tags=['Voucher - Admin']
    ),
    post=extend_schema(
        operation_id='admin_voucher_create',
        summary='Tạo mới voucher (Admin)',
        description='Quản trị viên tạo mới một chương trình voucher hoặc mã giảm giá cho hệ thống.',
        request=VoucherAdminWriteSerializer,
        responses={201: VoucherAdminSerializer},
        examples=[
            OpenApiExample(
                'Voucher giảm 20%',
                value={
                    'code': 'TESTFE20',
                    'name': 'Voucher kiểm thử FE',
                    'description': 'Giảm 20% khi kiểm thử',
                    'distribution_type': 'PUBLIC',
                    'discount_type': 'PERCENT',
                    'discount_value': '20.00',
                    'max_discount_amount': '50000.00',
                    'min_order_amount': '100000.00',
                    'issuance_limit': 100,
                    'start_at': '2026-09-01T00:00:00+07:00',
                    'end_at': '2026-12-31T23:59:59+07:00',
                    'is_active': True,
                },
                request_only=True,
            ),
        ],
        tags=['Voucher - Admin']
    ),
)

VOUCHER_ADMIN_DETAIL_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='admin_voucher_detail',
        summary='Chi tiết voucher (Admin)',
        description='Xem thông tin chi tiết cấu hình của một voucher theo ID.',
        tags=['Voucher - Admin']
    ),
    patch=extend_schema(
        operation_id='admin_voucher_update',
        summary='Cập nhật voucher (Admin)',
        description='Chỉnh sửa thông tin, thời gian hoặc cấu hình giảm giá của một voucher.',
        request=VoucherAdminWriteSerializer,
        responses={200: VoucherAdminSerializer},
        tags=['Voucher - Admin']
    ),
    delete=extend_schema(
        operation_id='admin_voucher_delete',
        summary='Ngừng sử dụng voucher (Admin)',
        description='Vô hiệu hóa (ngừng hoạt động) voucher bằng cách chuyển trạng thái is_active thành thành false.',
        tags=['Voucher - Admin']
    ),
)

VOUCHER_ADMIN_BY_CODE_SCHEMA = extend_schema_view(
    get=extend_schema(
        operation_id='admin_voucher_by_code_detail',
        summary='Tra cứu chi tiết voucher bằng mã code (Admin)',
        description='Tìm kiếm và lấy thông tin chi tiết của voucher trực tiếp thông qua chuỗi mã code.',
        tags=['Voucher - Admin']
    )
)

VOUCHER_ADMIN_ASSIGN_SCHEMA = extend_schema_view(
    post=extend_schema(
        operation_id='admin_voucher_assign',
        summary='Cấp voucher riêng cho khách hàng',
        description=(
            'Admin cấp một voucher có hình thức phát hành ASSIGNED cho một khách hàng. '
            'Mỗi khách hàng chỉ được nhận voucher đó một lần.'
        ),
        request=VoucherAdminAssignSerializer,
        responses={201: AdminAssignedUserVoucherSerializer},
        examples=[
            OpenApiExample(
                'Cấp voucher cho khách hàng',
                value={
                    'voucher_id': 12,
                    'customer_id': 15,
                    'note': 'Tặng khách hàng thân thiết',
                },
                request_only=True,
            ),
        ],
        tags=['Voucher - Admin'],
    ),
)
