from django.urls import path
from .views import (
    ReportOverviewView, ReportWorkersView, ReportServicesView,
    ReportRevenueView, ReportBookingsView, ReportExportView,
    MonthlyFavoriteReportView,
    MonthlyVipReportView, MonthlyVipRewardView,
    ReportAuditView,
)

urlpatterns = [
    path('reports/audit/', ReportAuditView.as_view(), name='admin-report-audit'),
    path('reports/vip-customers/', MonthlyVipReportView.as_view(), name='admin-report-vip-customers'),
    path('reports/vip-customers/reward/', MonthlyVipRewardView.as_view(), name='admin-report-vip-reward'),
    path('reports/favorite-workers/', MonthlyFavoriteReportView.as_view(), name='admin-report-favorite-workers'),
    path('reports/', ReportOverviewView.as_view(), name='admin-report-overview'),
    path('reports/workers/', ReportWorkersView.as_view(), name='admin-report-workers'),
    path('reports/services/', ReportServicesView.as_view(), name='admin-report-services'),
    path('reports/revenue/', ReportRevenueView.as_view(), name='admin-report-revenue'),
    path('reports/bookings/', ReportBookingsView.as_view(), name='admin-report-bookings'),
    path('reports/export/', ReportExportView.as_view(), name='admin-report-export'),
]
