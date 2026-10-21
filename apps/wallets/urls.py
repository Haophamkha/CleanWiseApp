from django.conf import settings
from django.urls import path

from .views import (
    MockTopupConfirmView,
    WalletDetailView,
    WalletTopupCreateView,
    WalletTopupDetailView,
    WalletTransactionListView,
    WalletWithdrawDetailView,
    WalletWithdrawRequestView,
    WalletWithdrawOtpView
)

urlpatterns = [
    path('wallet/', WalletDetailView.as_view(), name='customer-wallet-detail'),
    path('wallet/transactions/', WalletTransactionListView.as_view(), name='customer-wallet-transactions'),
    path('wallet/withdraw/', WalletWithdrawRequestView.as_view(), name='customer-wallet-withdraw'),
    path('wallet/withdraw/<int:pk>/', WalletWithdrawDetailView.as_view(), name='customer-wallet-withdraw-detail'),
    path('wallet/topup/', WalletTopupCreateView.as_view(), name='customer-wallet-topup'),
    path('wallet/topup/<int:pk>/', WalletTopupDetailView.as_view(), name='customer-wallet-topup-detail'),
    path('wallet/withdraw/otp/', WalletWithdrawOtpView.as_view(), name='customer-wallet-withdraw-otp'),
]

# Route giả lập thanh toán: KHÔNG tồn tại trên production.
if settings.DEBUG or getattr(settings, 'PAYOUT_ALLOW_MOCK', False):
    urlpatterns.append(
        path('wallet/topup/<int:pk>/mock-confirm/', MockTopupConfirmView.as_view(),
             name='customer-wallet-topup-mock-confirm'),
    )