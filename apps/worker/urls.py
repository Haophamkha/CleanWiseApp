from django.urls import path

from .views import (
    WorkerInvitationResponseView,
    WorkerActiveAreaListView,
    WorkerAvailableScheduleListView,
    WorkerCancelAssignmentView,
    WorkerClaimScheduleView,
    WorkerClaimBookingPackageView,
    WorkerMyScheduleListView,
    WorkerWorkingAreaView,
    WorkerCheckInView,
    WorkerCheckOutView,
    WorkerScheduleImageUploadView,
    WorkerBookingScheduleListView,
    WorkerActiveProvinceListView,
    WorkerDeclinePreferredView,
    WorkerConfirmCashView,
    WorkerMyScheduleCountsView,
)


urlpatterns = [
    path('invitations/<int:invitation_id>/respond/', WorkerInvitationResponseView.as_view(), name='worker-invitation-respond'),
    path('schedules/available/', WorkerAvailableScheduleListView.as_view(), name='worker-schedule-available'),
    path('schedules/my-schedules/', WorkerMyScheduleListView.as_view(), name='worker-schedule-my'),
    path('bookings/<int:booking_id>/schedules/', WorkerBookingScheduleListView.as_view(), name='worker-booking-schedules'),
    path(
        'bookings/<int:booking_id>/decline/',
        WorkerDeclinePreferredView.as_view(),
        name='worker-decline-preferred',
    ),
    path('schedules/<int:schedule_id>/claim/', WorkerClaimScheduleView.as_view(), name='worker-schedule-claim'),
    path('bookings/<int:booking_id>/claim/', WorkerClaimBookingPackageView.as_view(), name='worker-booking-claim-package'),
    path('assignments/<int:assignment_id>/cancel/', WorkerCancelAssignmentView.as_view(), name='worker-assignment-cancel'),
    path('areas/', WorkerActiveAreaListView.as_view(), name='worker-active-area-list'),
    path('working-areas/', WorkerWorkingAreaView.as_view(), name='worker-working-area'),
    path('schedules/<int:schedule_id>/check-in/', WorkerCheckInView.as_view(), name='worker-schedule-check-in'),
    path('schedules/<int:schedule_id>/check-out/', WorkerCheckOutView.as_view(), name='worker-schedule-check-out'),
    path('schedules/<int:schedule_id>/images/', WorkerScheduleImageUploadView.as_view(), name='worker-schedule-image-upload'),
    path('provinces/', WorkerActiveProvinceListView.as_view(), name='worker-active-province-list'),
    path('schedules/<int:schedule_id>/confirm-cash/', WorkerConfirmCashView.as_view(), name='worker-schedule-confirm-cash'),
    path('schedules/my-schedules/counts/', WorkerMyScheduleCountsView.as_view(), name='worker-schedule-my-counts'),
]