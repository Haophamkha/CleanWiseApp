from django.utils import timezone
from django.db import transaction

from apps.worker.constants import CHECKIN_MISSED_GRACE_MINUTES
from .models import Notification
from .push import send_push_to_user
from .realtime import push_unread_count


# ===================== HELPERS =====================

def fmt_vnd(amount) -> str:
    return f"{int(amount):,}".replace(",", ".") + "đ"


def fmt_time(dt) -> str:
    return timezone.localtime(dt).strftime('%H:%M, %d/%m')


def code(booking) -> str:
    return f"#{booking.booking_code}"


def display_name(user) -> str:
    return user.get_full_name() or user.username


def create_notification(user, title, message, type=Notification.Type.SYSTEM,
                        related_booking=None, related_schedule=None, navigation_source='mine',
                        target=''):
    target = target or ('booking' if related_booking else 'none')
    notif = Notification.objects.create(
        user=user, title=title, message=message, type=type, navigation_source=navigation_source,
        related_booking=related_booking, related_schedule=related_schedule, target=target,
    )
    push_unread_count(user.id)
    return notif


def create_notification_with_push(user, title, message, type=Notification.Type.SYSTEM,
                                  related_booking=None, related_schedule=None,
                                  extra_data=None, target=''):
    notif = create_notification(user, title, message, type, related_booking, related_schedule,
                                navigation_source=(extra_data or {}).get('source', 'mine'),
                                target=target)
    payload = {
        'notification_id': notif.id,
        'type': type,
        'target': notif.target,
        'booking_id': related_booking.id if related_booking else None,
        'schedule_id': related_schedule.id if related_schedule else None,
        **(extra_data or {}),
    }
    transaction.on_commit(
        lambda: send_push_to_user(user, title, message, payload),
        robust=True,
    )
    return notif


# ===================== CUSTOMER =====================

def notify_booking_success(booking):
    from apps.bookings.expiry_service import UNPAID_BOOKING_TTL_MINUTES
    from apps.payments.models import Payment

    first = booking.schedules.order_by('scheduled_start').first()
    message = f'Đơn {code(booking)} đã được tạo'
    if first:
        message += f', buổi đầu tiên lúc {fmt_time(first.scheduled_start)}'
    message += '.'
    if (
        booking.payment_status == 'UNPAID'
        and booking.payments.filter(
            method=Payment.Method.BANK_TRANSFER,
            status=Payment.Status.PENDING,
        ).exists()
    ):
        message += (
            f' Vui lòng chuyển khoản trong {UNPAID_BOOKING_TTL_MINUTES} phút, '
            'quá hạn đơn sẽ tự hủy.'
        )
    elif booking.payment_status == 'PAID':
        message += ' Chúng tôi đang tìm nhân viên cho bạn.'
    return create_notification_with_push(
        user=booking.customer,
        title='Đặt lịch thành công',
        message=message,
        type=Notification.Type.BOOKING,
        related_booking=booking,
    )


_METHOD = {'CASH': 'tiền mặt', 'BANK_TRANSFER': 'chuyển khoản', 'WALLET': 'ví CleanWise'}


def notify_payment_success(payment):
    how = _METHOD.get(payment.method, '')
    return create_notification_with_push(
        user=payment.customer, title='Thanh toán thành công',
        message=f'Đã thanh toán {fmt_vnd(payment.amount)}' + (f' bằng {how}' if how else '')
                + f' cho đơn {code(payment.booking)}.',
        type=Notification.Type.PAYMENT, related_booking=payment.booking,
    )


def notify_payment_failed(payment):
    why = (payment.failure_reason or '').strip()
    return create_notification_with_push(
        user=payment.customer, title='Thanh toán thất bại',
        message=f'Đơn {code(payment.booking)} chưa thanh toán được.'
                + (f' Lý do: {why}' if why else '') + ' Vui lòng thử lại.',
        type=Notification.Type.PAYMENT, related_booking=payment.booking,
    )


def notify_customer_worker_assigned(assignment):
    schedule = assignment.schedule
    booking = schedule.booking
    preferred = schedule.preferred_worker_id == assignment.worker_id
    who = display_name(assignment.worker)
    return create_notification_with_push(
        user=booking.customer,
        title='Nhân viên bạn chọn đã nhận việc' if preferred else 'Đã có nhân viên nhận việc',
        message=f'{who} đã nhận buổi {schedule.sequence_no} của đơn {code(booking)}.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking,
    )


def notify_customer_worker_assigned_batch(booking, worker, schedule_count, preferred=False):
    """Worker nhận nhiều buổi cùng lúc — gộp thành 1 thông báo."""
    return create_notification_with_push(
        user=booking.customer,
        title='Nhân viên bạn chọn đã nhận việc' if preferred else 'Đã có nhân viên nhận việc',
        message=f'{display_name(worker)} đã nhận {schedule_count} buổi của đơn {code(booking)}.',
        type=Notification.Type.ASSIGNMENT,
        related_booking=booking,
    )


def notify_customer_worker_cancelled_schedule(schedule, reason):
    booking = schedule.booking
    return create_notification_with_push(
        user=booking.customer,
        title='Nhân viên đã hủy buổi làm',
        message=f'Buổi {schedule.sequence_no} của đơn {code(booking)} đã bị hủy ({reason}). '
                f'Chúng tôi đang tìm nhân viên khác cho bạn.',
        type=Notification.Type.ASSIGNMENT,
        related_booking=booking,
    )


def notify_customer_booking_cancelled(booking):
    reason = (booking.cancel_reason or '').strip()
    by = booking.cancelled_by
    if by is None:
        title, head = 'Đơn đã tự động hủy', f'Đơn {code(booking)} đã tự động hủy.'
    elif getattr(by, 'role', '') == 'ADMIN':
        title, head = 'CleanWise đã hủy đơn', f'CleanWise đã hủy đơn {code(booking)}.'
    else:
        title, head = 'Đơn đã bị hủy', f'Đơn {code(booking)} đã bị hủy.'
    message = head + (f' Lý do: {reason}' if reason else '')
    if booking.payment_status == 'PAID':
        message += ' Tiền các buổi chưa thực hiện sẽ được hoàn vào ví CleanWise.'
    return create_notification_with_push(
        user=booking.customer, title=title, message=message,
        type=Notification.Type.BOOKING, related_booking=booking,
    )

def notify_customer_booking_failed(booking):
    first = booking.schedules.order_by('scheduled_start').first()
    when = f' (giờ làm {fmt_time(first.scheduled_start)})' if first else ''
    message = f'Đơn {code(booking)}{when} không có nhân viên nhận trước giờ làm nên đã bị hủy.'
    if booking.payment_status == 'PAID':
        message += ' Tiền sẽ được hoàn vào ví CleanWise.'
    return create_notification_with_push(
        user=booking.customer,
        title='Chưa tìm được nhân viên',
        message=message,
        type=Notification.Type.BOOKING,
        related_booking=booking,
    )


def notify_customer_schedule_reminder(schedule):
    return create_notification_with_push(
        user=schedule.booking.customer,
        title='Sắp đến giờ dọn dẹp',
        message=f'Lịch dọn dẹp của bạn bắt đầu lúc {fmt_time(schedule.scheduled_start)}.',
        type=Notification.Type.BOOKING,
        related_booking=schedule.booking,
    )


def notify_customer_booking_completed(booking):
    return create_notification_with_push(
        user=booking.customer,
        title='Đơn đã hoàn thành',
        message=f'Đơn {code(booking)} đã hoàn thành. Hãy đánh giá dịch vụ nhé!',
        type=Notification.Type.BOOKING,
        related_booking=booking,
    )


def notify_complaint_resolved(complaint):
    note = (complaint.resolution_note or '').strip()
    outcome = complaint.outcome
    if complaint.reporter_role == 'CUSTOMER':
        tail = {
            'REFUND_CUSTOMER': f'Bạn được hoàn {fmt_vnd(complaint.refund_amount)} vào ví CleanWise.',
            'PAY_WORKER': 'Sau khi xem xét, buổi làm được ghi nhận là đã thực hiện nên không hoàn tiền.',
        }.get(outcome, '')
    else:
        tail = {
            'PAY_WORKER': f'Thu nhập {fmt_vnd(complaint.worker_delta)} đã được cộng vào ví của bạn.',
            'REFUND_CUSTOMER': 'Sau khi xem xét, CleanWise đã hoàn tiền cho khách nên buổi này không tính thu nhập.',
        }.get(outcome, '')
    message = f'Khiếu nại {complaint.code} cho đơn {code(complaint.booking)} đã được giải quyết.'
    if tail:
        message += f' {tail}'
    if note:
        message += f' Ghi chú: {note}'
    return create_notification_with_push(
        user=complaint.reporter, title='Khiếu nại đã được xử lý', message=message,
        type=Notification.Type.COMPLAINT, related_booking=complaint.booking,
    )


def notify_complaint_rejected(complaint):
    note = (complaint.resolution_note or '').strip()
    message = f'Khiếu nại {complaint.code} cho đơn {code(complaint.booking)} không được chấp nhận.'
    if note:
        message += f' Lý do: {note}'
    return create_notification_with_push(
        user=complaint.reporter, title='Khiếu nại bị từ chối', message=message,
        type=Notification.Type.COMPLAINT, related_booking=complaint.booking,
    )


def notify_customer_preferred_declined(booking, worker):
    return create_notification_with_push(
        user=booking.customer, title='Nhân viên bạn chọn đã từ chối',
        message=f'{display_name(worker)} đã từ chối đơn {code(booking)}. '
                f'Đơn đã mở cho các nhân viên khác, bạn không cần làm gì thêm.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking,
    )


def notify_customer_preferred_expired(booking, worker):
    return create_notification_with_push(
        user=booking.customer, title='Nhân viên bạn chọn chưa phản hồi',
        message=f'{display_name(worker)} chưa phản hồi đơn {code(booking)} trong thời gian giữ chỗ. '
                f'Đơn đã mở cho các nhân viên khác.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking,
    )


def notify_customer_preferred_withdrawn(booking, worker, schedule):
    return create_notification_with_push(
        user=booking.customer, title='Lời chỉ định đã được thu hồi',
        message=f'CleanWise đã thu hồi lời chỉ định {display_name(worker)} cho buổi {schedule.sequence_no} '
                f'của đơn {code(booking)}. Đơn đã mở cho các nhân viên khác.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking, related_schedule=schedule,
    )


def notify_customer_worker_checked_in(schedule, worker):
    booking = schedule.booking
    return create_notification_with_push(
        user=booking.customer, title='Nhân viên đã bắt đầu làm',
        message=f'{display_name(worker)} đã đến và bắt đầu buổi {schedule.sequence_no} của đơn {code(booking)}.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking,
    )


def notify_customer_schedule_completed(schedule, automatic=False):
    booking = schedule.booking
    tail = ' (hệ thống tự xác nhận do nhân viên chưa check-out)' if automatic else ''
    return create_notification_with_push(
        user=booking.customer, title='Buổi làm đã hoàn thành',
        message=f'Buổi {schedule.sequence_no} của đơn {code(booking)} đã hoàn thành{tail}.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking,
    )


def notify_customer_worker_no_show(schedule, refunded):
    booking = schedule.booking
    message = (
        f'Nhân viên không check-in đúng giờ buổi {schedule.sequence_no} của đơn {code(booking)} '
        f'(giờ hẹn {fmt_time(schedule.scheduled_start)}) nên buổi này đã bị hủy.'
    )
    if refunded:
        message += ' Tiền buổi này sẽ được hoàn vào ví CleanWise.'
    return create_notification_with_push(
        user=booking.customer, title='Buổi làm bị hủy do nhân viên vắng mặt', message=message,
        type=Notification.Type.ASSIGNMENT, related_booking=booking, related_schedule=schedule,
    )


def notify_worker_no_show(schedule, worker):
    return create_notification_with_push(
        user=worker, title='Bạn đã bị gỡ khỏi buổi làm',
        message=f'Bạn không check-in buổi {schedule.sequence_no} đơn {code(schedule.booking)} '
                f'trong {CHECKIN_MISSED_GRACE_MINUTES} phút sau giờ hẹn.',
        type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking, related_schedule=schedule,
    )


def notify_customer_schedule_missed(schedule):
    booking = schedule.booking
    return create_notification_with_push(
        user=booking.customer, title='Buổi làm đã hết hạn',
        message=f'Buổi {schedule.sequence_no} của đơn {code(booking)} (giờ hẹn {fmt_time(schedule.scheduled_start)}) '
                f'đã bị hủy vì đến giờ làm vẫn chưa có nhân viên nhận.',
        type=Notification.Type.BOOKING, related_booking=booking, related_schedule=schedule,
    )


def notify_customer_worker_removed(schedule, worker):
    booking = schedule.booking
    return create_notification_with_push(
        user=booking.customer, title='Nhân viên không thể thực hiện buổi làm',
        message=f'{display_name(worker)} không thể thực hiện buổi {schedule.sequence_no} của đơn {code(booking)}. '
                f'CleanWise đang tìm nhân viên khác cho bạn.',
        type=Notification.Type.ASSIGNMENT, related_booking=booking, related_schedule=schedule,
    )


# ===================== WORKER =====================

def notify_worker_new_job(assignment):
    booking = assignment.schedule.booking
    return create_notification_with_push(
        user=assignment.worker,
        title='Bạn có lịch mới',
        message=f'Đơn {code(booking)} bắt đầu lúc {fmt_time(assignment.schedule.scheduled_start)}.',
        type=Notification.Type.ASSIGNMENT,
        related_booking=booking,
    )


def notify_worker_removed_from_schedule(schedule, worker, reason=''):
    message = f'CleanWise đã gỡ bạn khỏi buổi {schedule.sequence_no} của đơn {code(schedule.booking)}.'
    if reason:
        message += f' Lý do: {reason}'
    return create_notification_with_push(
        user=worker, title='Bạn đã được gỡ khỏi lịch làm', message=message,
        type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking, related_schedule=schedule,
        target='none',
    )


def notify_worker_booking_cancelled(schedule, worker, by_admin=False, reason=''):
    who = 'CleanWise' if by_admin else 'khách hàng'
    msg = f'Đơn {code(schedule.booking)} đã bị {who} hủy.' + (f' Lý do: {reason}' if reason else '')
    return create_notification_with_push(
        user=worker, title=f'Đơn đã bị {who} hủy', message=msg,
        type=Notification.Type.BOOKING, related_booking=schedule.booking,
        related_schedule=schedule,
        target='none',
    )


def notify_worker_schedule_reminder(schedule, worker):
    return create_notification_with_push(
        user=worker,
        title='Sắp đến giờ làm',
        message=f'Đơn {code(schedule.booking)} bắt đầu lúc {fmt_time(schedule.scheduled_start)}.',
        type=Notification.Type.BOOKING,
        related_booking=schedule.booking,
    )


def notify_worker_schedule_cancelled(schedule, worker):
    return create_notification_with_push(
        user=worker,
        title='Khách đã hủy buổi làm',
        message=f'Buổi {schedule.sequence_no} của đơn {code(schedule.booking)} đã bị khách hàng hủy.',
        type=Notification.Type.ASSIGNMENT,
        related_booking=schedule.booking,
        related_schedule=schedule,
        target='none',
    )


def notify_worker_profile_approved(worker_profile):
    return create_notification_with_push(
        user=worker_profile.user,
        title='Hồ sơ đã được duyệt 🎉',
        message='CleanWise đã phê duyệt hồ sơ của bạn. Bạn có thể bắt đầu nhận lịch.',
        type=Notification.Type.SYSTEM,
    )


def notify_worker_profile_rejected(worker_profile):
    reason = (worker_profile.rejection_reason or '').strip()
    return create_notification_with_push(
        user=worker_profile.user,
        title='Hồ sơ chưa được duyệt',
        message=f'Hồ sơ của bạn chưa được duyệt. {reason}'.strip(),
        type=Notification.Type.SYSTEM,
    )
    

def notify_worker_preferred_request(booking, worker, expires_at):
    first_schedule = (
        booking.schedules.filter(status='PENDING').order_by('scheduled_start').first()
    )
    message = f'{display_name(booking.customer)} muốn bạn làm đơn {code(booking)}.'
    if expires_at > timezone.now():
        message += f' Nhận việc trước {fmt_time(expires_at)} để giữ đơn.'
    return create_notification_with_push(
        user=worker,
        title='Khách chỉ định bạn',
        message=message,
        type=Notification.Type.ASSIGNMENT,
        related_booking=booking,
        related_schedule=first_schedule,
        extra_data={'source': 'available'},
    )


# ===================== WALLET / ADMIN =====================

def notify_customer_refund(booking, amount, reason=None):
    message = f'{fmt_vnd(amount)} đã được hoàn vào ví CleanWise. Đơn {code(booking)}.'
    if reason:
        message += f' Lý do: {reason}'
    return create_notification_with_push(
        user=booking.customer,
        title='Hoàn tiền thành công',
        message=message,
        type=Notification.Type.PAYMENT,
        related_booking=booking,
        target='wallet',
    )


def notify_wallet_adjustment(user, amount, direction, reason):
    sign = '+' if direction == 'CREDIT' else '-'
    return create_notification_with_push(
        user=user,
        title='Số dư ví được điều chỉnh',
        message=f'{sign}{fmt_vnd(amount)}. Lý do: {reason}',
        type=Notification.Type.PAYMENT,
        target='wallet',
    )


def notify_withdraw_result(withdraw):
    destination = f'{withdraw.bank_name} ****{withdraw.account_number_last4}'
    if withdraw.status == 'SUCCESS':
        title = 'Rút tiền thành công'
        message = f'Đã chuyển {fmt_vnd(withdraw.amount)} về {destination}.'
    else:
        why = (withdraw.failure_reason or '').strip()
        title = 'Rút tiền không thành công'
        message = (
            f'Yêu cầu rút {fmt_vnd(withdraw.amount)} về {destination} không thành công, '
            'tiền đã hoàn lại ví.'
        )
        if why:
            message += f' Lý do: {why}'
    return create_notification_with_push(
        user=withdraw.user, title=title, message=message,
        type=Notification.Type.PAYMENT, target='wallet',
    )


def notify_admins_withdraw_review(withdraw_id, note):
    from django.contrib.auth import get_user_model

    admins = get_user_model().objects.filter(role='ADMIN', is_active=True)
    for admin in admins:
        create_notification(
            admin, 'Lệnh rút tiền cần đối soát',
            f'Lệnh rút #{withdraw_id} cần đối soát tay. {note}',
            type=Notification.Type.PAYMENT, target='wallet',
        )


def notify_admin_announcement(users, title, message):
    notifs = Notification.objects.bulk_create([
        Notification(user=user, title=title, message=message, type=Notification.Type.SYSTEM)
        for user in users
    ])
    for user in users:
        send_push_to_user(user, title, message)
        push_unread_count(user.id)
    return notifs


def notify_worker_cash_reminder(booking, schedule, worker):
    return create_notification_with_push(
        user=worker,
        title='Nhắc xác nhận thu tiền mặt',
        message=f'Đơn {code(booking)} đã xong. Hãy xác nhận đã thu tiền mặt từ khách.',
        type=Notification.Type.PAYMENT,
        related_booking=booking,
        related_schedule=schedule,
    )