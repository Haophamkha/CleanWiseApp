from rest_framework.permissions import BasePermission

CHATBOT_ROLES = ('CUSTOMER', 'WORKER')


class IsChatbotUser(BasePermission):
    message = 'Trợ lý chỉ hỗ trợ tài khoản khách hàng hoặc nhân viên.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user.is_authenticated and user.is_active and user.role in CHATBOT_ROLES)


# Giữ tên cũ để code khác (nếu có) không vỡ.
IsChatbotCustomer = IsChatbotUser