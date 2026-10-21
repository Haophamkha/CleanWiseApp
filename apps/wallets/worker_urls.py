from django.conf import settings
from django.urls import path

from .views import (
    MockTopupConfirmView,
    WorkerWalletDetailView,
    WorkerWalletTopupCreateView,
    WorkerWalletTopupDetailView,
    WorkerWalletTransactionListView,
    WorkerWalletWithdrawDetailView,
    WorkerWalletWithdrawRequestView,
    WorkerWalletWithdrawOtpView
)
from .worker_earnings import WorkerEarningHistoryView, WorkerEarningSummaryView

urlpatterns = [
    path('earnings/summary/', WorkerEarningSummaryView.as_view(), name='worker-earnings-summary'),
    path('earnings/history/', WorkerEarningHistoryView.as_view(), name='worker-earnings-history'),

    path('wallet/', WorkerWalletDetailView.as_view(), name='worker-wallet-detail'),
    path('wallet/transactions/', WorkerWalletTransactionListView.as_view(), name='worker-wallet-transactions'),
    path('wallet/withdraw/', WorkerWalletWithdrawRequestView.as_view(), name='worker-wallet-withdraw'),
    path('wallet/withdraw/<int:pk>/', WorkerWalletWithdrawDetailView.as_view(), name='worker-wallet-withdraw-detail'),
    path('wallet/topup/', WorkerWalletTopupCreateView.as_view(), name='worker-wallet-topup'),
    path('wallet/topup/<int:pk>/', WorkerWalletTopupDetailView.as_view(), name='worker-wallet-topup-detail'),
    path('wallet/withdraw/otp/', WorkerWalletWithdrawOtpView.as_view(), name='worker-wallet-withdraw-otp'),
]

# Route giả lập thanh toán: KHÔNG tồn tại trên production.
if settings.DEBUG or getattr(settings, 'PAYOUT_ALLOW_MOCK', False):
    urlpatterns.append(
        path('wallet/topup/<int:pk>/mock-confirm/', MockTopupConfirmView.as_view(),
             name='worker-wallet-topup-mock-confirm'),
    )