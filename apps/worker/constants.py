from django.conf import settings


MIN_CANCEL_HOURS = 6
AUTO_CANCEL_NOTE = 'Tự động hủy do nhân viên không check-in đúng hạn.'
CHECKIN_MISSED_GRACE_MINUTES = getattr(settings, 'CHECKIN_MISSED_GRACE_MINUTES', 30)