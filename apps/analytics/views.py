from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiTypes

from apps.common.permissions import IsAdminRole
from apps.common.xlsx import workbook_bytes
from .report_service import Report, ReportParams, ZONE
from .favorite_report import FavoriteReportParams, monthly_favorite_report
from .vip_report import VipReportParams, VipRewardInput, monthly_vip_report, reward_vip_customers


class MonthlyVipReportView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(parameters=[VipReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        params = VipReportParams(data=request.query_params)
        params.is_valid(raise_exception=True)
        return Response({'data': monthly_vip_report(params.validated_data)})


class MonthlyVipRewardView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(request=VipRewardInput, responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def post(self, request):
        params = VipRewardInput(data=request.data)
        params.is_valid(raise_exception=True)
        return Response({'data': reward_vip_customers(params.validated_data, request.user)})


class MonthlyFavoriteReportView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(parameters=[FavoriteReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        params = FavoriteReportParams(data=request.query_params)
        params.is_valid(raise_exception=True)
        return Response({'data': monthly_favorite_report(params.validated_data)})


class ReportView(APIView):
    permission_classes = [IsAdminRole]

    def report(self, request):
        params = ReportParams(data=request.query_params)
        params.is_valid(raise_exception=True)
        return Report(params.validated_data)

    def paginated(self, report, rows, mapper=lambda row: row):
        size, page = report.params['page_size'], report.params['page']
        count = len(rows) if isinstance(rows, list) else rows.count()
        total_pages = (count + size - 1) // size
        if page > max(1, total_pages):
            raise serializers.ValidationError({'page': 'Trang không tồn tại.'})
        return {'period': report.period(), 'results': [mapper(row) for row in rows[(page - 1) * size:page * size]],
            'count': count, 'page': page, 'page_size': size, 'total_pages': total_pages,
            'has_next': page < total_pages, 'has_previous': page > 1}


class ReportOverviewView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        return Response({'data': {'period': report.period(), 'summary': report.summary(),
            'timeline': report.timeline(), 'services': report.services(),
            'top_workers': report.workers()[:10],
            'recent_bookings': [report.booking_row(row) for row in report.booking_queryset()[:10]]}})


class ReportWorkersView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        return Response({'data': self.paginated(report, report.workers())})


class ReportServicesView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        return Response({'data': self.paginated(report, report.services())})


class ReportRevenueView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        return Response({'data': self.paginated(report, report.revenue_queryset(), report.revenue_row)})


class ReportBookingsView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        return Response({'data': self.paginated(report, report.booking_queryset(), report.booking_row)})


class ReportAuditView(ReportView):
    @extend_schema(parameters=[ReportParams], responses=OpenApiTypes.OBJECT, tags=['Admin reports'])
    def get(self, request):
        from apps.bookings.models import BookingSchedule
        report = self.report(request)
        kind = request.query_params.get('kind', 'missing_earning')
        if kind == 'missing_earning':
            rows = report.completed.filter(worker_earning__isnull=True)
            issue = 'Thiếu sổ thu nhập'
        elif kind == 'missing_end':
            rows = BookingSchedule.objects.filter(status='COMPLETED', actual_end__isnull=True)
            issue = 'Thiếu giờ kết thúc · Toàn hệ thống'
        else:
            raise serializers.ValidationError({'kind': 'Loại đối soát không hợp lệ.'})
        rows = rows.select_related('booking').order_by('-scheduled_start', '-id')
        return Response({'data': self.paginated(report, rows, lambda row: {
            'schedule_id': row.id, 'booking_id': row.booking_id, 'booking_code': row.booking.booking_code,
            'sequence_no': row.sequence_no, 'scheduled_start': row.scheduled_start,
            'actual_end': row.actual_end, 'issue': issue,
        })})


class ReportExportView(ReportView):
    @extend_schema(parameters=[ReportParams], responses={(200, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'): OpenApiTypes.BINARY}, tags=['Admin reports'])
    def get(self, request):
        report = self.report(request)
        if report.earnings.count() > 50000:
            raise serializers.ValidationError('Báo cáo vượt 50.000 buổi; chọn kỳ nhỏ hơn để xuất Excel.')
        summary = [['Chỉ số', 'Giá trị'], ['Kỳ', report.params['period']], ['Từ ngày', report.start.isoformat()],
            ['Đến ngày (không bao gồm)', report.end.isoformat()], ['Múi giờ', str(ZONE)],
            ['Cơ sở doanh thu', 'Hoa hồng đã ghi sổ, chưa trừ điều chỉnh hoàn tiền'],
            ['Cơ sở yêu thích', 'Số khách đang lưu nhân viên hiện tại, không giới hạn theo kỳ'],
            ['Cơ sở khiếu nại', 'Khách gửi trong kỳ, gắn nhân viên, không gồm đã hủy; đã xử lý không đồng nghĩa nhân viên có lỗi']]
        labels = {'total_orders': 'Tổng số đơn', 'total_order_value': 'Tổng giá trị đơn (VND)',
            'cancelled_failed_order_value': 'Giá trị đơn hủy/thất bại (VND)', 'valid_order_value': 'Giá trị đơn còn lại (VND)',
            'cleanwise_revenue': 'Hoa hồng CleanWise (VND)', 'completed_service_value': 'Giá trị buổi đã ghi sổ (VND)',
            'worker_income': 'Thu nhập nhân viên (VND)', 'completed_sessions': 'Buổi hoàn thành',
            'processing_orders': 'Đơn đang xử lý trong kỳ', 'active_workers_current': 'Nhân viên hoạt động hiện tại',
            'cash_commission_owed_in_period': 'Hoa hồng tiền mặt chưa thu trong kỳ (VND)',
            'cash_commission_owed_current': 'Hoa hồng tiền mặt chưa thu hiện tại (VND)',
            'missing_earning_sessions': 'Buổi trong kỳ thiếu sổ thu nhập',
            'completed_sessions_missing_actual_end': 'Buổi hoàn thành thiếu thời điểm kết thúc (toàn hệ thống)',
            'commission_on_refunded_bookings': 'Hoa hồng trên đơn đã hoàn tiền cần đối soát (VND)'}
        totals = report.summary()
        for key, label in labels.items():
            value = totals[key]
            summary.append([label, Decimal(value) if isinstance(value, str) else value])
        summary.extend([[f'Đơn {status}', count] for status, count in totals['order_statuses'].items()])
        timeline = [['Từ ngày', 'Đến ngày (không bao gồm)', 'Số đơn', 'Giá trị đơn (VND)', 'Hoa hồng (VND)', 'Giá trị buổi (VND)']]
        timeline.extend([[r['start'], r['end'], r['orders'], Decimal(r['order_value']), Decimal(r['cleanwise_revenue']), Decimal(r['completed_service_value'])] for r in report.timeline()])
        workers = [['Hạng', 'ID', 'Nhân viên', 'Đang hoạt động', 'Đơn có buổi hoàn thành', 'Buổi hoàn thành', 'Điểm trung bình', 'Lượt đánh giá', 'Khách yêu thích hiện tại', 'Khiếu nại khách gửi trong kỳ không gồm đã hủy', 'Khiếu nại đang xử lý', 'Khiếu nại đã xử lý', 'Khiếu nại bị từ chối', 'Hoa hồng (VND)', 'Thu nhập (VND)']]
        workers.extend([[r['rank'], r['worker_id'], r['name'], 'Có' if r['active_current'] else 'Không', r['completed_orders'], r['completed_sessions'], r['average_rating'], r['review_count'], r['favorite_count_current'], r['complaint_count'], r['complaint_pending_count'], r['complaint_resolved_count'], r['complaint_rejected_count'], Decimal(r['cleanwise_revenue']), Decimal(r['worker_income'])] for r in report.workers()])
        services = [['ID', 'Dịch vụ', 'Số đơn', 'Tỉ lệ đơn (%)', 'Đơn hủy', 'Đơn thất bại', 'Buổi hoàn thành', 'Giá trị đơn (VND)', 'Giá trị buổi (VND)', 'Hoa hồng (VND)']]
        services.extend([[r['service_id'], r['name'], r['orders'], r['order_share_percent'], r['cancelled_orders'], r['failed_orders'], r['completed_sessions'], Decimal(r['order_value']), Decimal(r['completed_service_value']), Decimal(r['cleanwise_revenue'])] for r in report.services()])
        def detail_rows():
            yield ['Ngày hoàn thành (giờ VN)', 'Mã đơn', 'Buổi', 'Dịch vụ', 'Nhân viên', 'Thanh toán', 'Giá trị buổi (VND)', 'Tỉ lệ hoa hồng', 'Hoa hồng (VND)', 'Thu nhập (VND)', 'Hoa hồng tiền mặt chưa thu', 'Ngày thu hoa hồng (giờ VN)', 'Trạng thái thanh toán đơn']
            for earning in report.revenue_queryset().iterator(chunk_size=1000):
                yield [timezone.localtime(earning.completed_at, ZONE).isoformat(), earning.booking.booking_code,
                    earning.schedule.sequence_no, earning.booking.service.name, earning.worker.get_full_name() or earning.worker.username,
                    earning.payment_method, earning.gross_amount, earning.commission_rate, earning.commission_amount, earning.worker_amount,
                    'Có' if earning.payment_method == 'CASH' and earning.settled_at is None else 'Không',
                    timezone.localtime(earning.settled_at, ZONE).isoformat() if earning.settled_at else '', earning.booking.payment_status]
        content = workbook_bytes([('Tổng hợp', summary), ('Theo thời gian', timeline), ('Nhân viên', workers), ('Dịch vụ', services), ('Chi tiết doanh thu', detail_rows())])
        response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="cleanwise-report-{report.start}-{report.end}.xlsx"'
        response['Cache-Control'] = 'no-store'
        return response
