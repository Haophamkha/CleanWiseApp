from django.urls import path

from .views import (
    CustomerBankCatalogView,
    CustomerPaymentMethodDetailView,
    CustomerPaymentMethodListCreateView,
    CustomerPaymentMethodOptionsView,
    CustomerPaymentMethodSetDefaultView,
    CustomerPaymentMethodVerificationView,
    CustomerPaymentMethodVerificationConfirmView,
    PayOSWebhookView,
    CustomerPaymentHistoryView,
)


urlpatterns = [
    path('payment-methods/', CustomerPaymentMethodListCreateView.as_view(), name='customer-payment-method-list-create'),
    path('payment-methods/options/', CustomerPaymentMethodOptionsView.as_view(), name='customer-payment-method-options'),
    path('payment-methods/banks/', CustomerBankCatalogView.as_view(), name='customer-payment-method-banks'),
    path('payment-methods/<int:pk>/', CustomerPaymentMethodDetailView.as_view(), name='customer-payment-method-detail'),
    path('payment-methods/<int:pk>/default/', CustomerPaymentMethodSetDefaultView.as_view(), name='customer-payment-method-set-default'),
    path('payment-methods/<int:pk>/verification/', CustomerPaymentMethodVerificationView.as_view(), name='customer-payment-method-verification'),
    path('payment-methods/<int:pk>/verification/confirm/', CustomerPaymentMethodVerificationConfirmView.as_view(), name='customer-payment-method-verification-confirm'),
    path('webhooks/payos/', PayOSWebhookView.as_view(), name='payos-webhook'),
    path('payments/history/', CustomerPaymentHistoryView.as_view(), name='customer-payment-history'),
]
