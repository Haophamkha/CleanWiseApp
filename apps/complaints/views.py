# apps/complaints/views.py

from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from rest_framework.parsers import MultiPartParser, FormParser

from .models import Complaint, ComplaintAttachment, ComplaintIssueType

from apps.common.permissions import (
    IsAdminRole,
    IsCustomerOrWorkerRole,
    get_user_role,
)

from apps.notifications.realtime import push_complaint_changed

from .permissions import IsComplaintOwnerOrAdmin

from .schemas import (
    COMPLAINT_CANCEL_SCHEMA,
    COMPLAINT_LIST_CREATE_SCHEMA,
    COMPLAINT_DETAIL_SCHEMA,
    COMPLAINT_ISSUE_TYPE_SCHEMA,
    COMPLAINT_RESOLVE_SCHEMA,
)

from .serializers import (
    ComplaintCancelSerializer,
    ComplaintCreateSerializer,
    ComplaintDetailSerializer,
    ComplaintIssueTypeSerializer,
    ComplaintListSerializer,
    ComplaintResolveSerializer,
    ComplaintAttachmentSerializer,
)


ALLOWED_IMAGE_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
)

MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_ATTACHMENTS = 5


@COMPLAINT_ISSUE_TYPE_SCHEMA
class ComplaintIssueTypeListView(generics.ListAPIView):
    """
    Khách hàng hoặc nhân viên lấy danh sách loại sự cố để lựa chọn.
    Chỉ trả về loại áp dụng đúng vai trò của người gọi (ANY luôn).
    """

    permission_classes = [
        IsCustomerOrWorkerRole,
    ]

    serializer_class = ComplaintIssueTypeSerializer

    def get_queryset(self):
        role = get_user_role(self.request.user)

        queryset = ComplaintIssueType.objects.filter(
            is_active=True,
            applies_to__in=[
                role,
                ComplaintIssueType.AppliesTo.ANY,
            ],
        )

        stage = self.request.query_params.get("stage")

        if stage:
            queryset = queryset.filter(
                stage__in=[
                    stage,
                    ComplaintIssueType.Stage.ANY,
                ]
            )

        return queryset


class _AnyAuthenticatedRoleForList(IsCustomerOrWorkerRole):
    """
    GET danh sách: khách, nhân viên hoặc admin đều gọi được
    (lọc theo quyền nằm trong get_queryset).

    Tách riêng để không phải sửa IsCustomerOrWorkerRole dùng chung
    ở chỗ khác.
    """

    allowed_roles = (
        "CUSTOMER",
        "WORKER",
        "ADMIN",
    )


@COMPLAINT_LIST_CREATE_SCHEMA
class ComplaintListCreateView(generics.ListCreateAPIView):
    """
    Dùng chung cho cả app khách hàng và app nhân viên.

    GET chỉ trả về khiếu nại của chính người gọi
    (trừ ADMIN thấy hết).

    POST tạo khiếu nại với reporter_role suy từ role
    của người gọi (xem serializer).
    """

    parser_classes = [
        MultiPartParser,
        FormParser,
    ]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsCustomerOrWorkerRole()]

        return [_AnyAuthenticatedRoleForList()]

    def get_queryset(self):
        queryset = Complaint.objects.select_related(
            "reporter",
            "worker",
            "booking",
            "issue_type",
            "resolved_by",
            "schedule",
        ).prefetch_related("attachments")

        schedule_filter = self.request.query_params.get("schedule")

        user = self.request.user
        role = get_user_role(user)

        if role == "ADMIN" or user.is_superuser:
            status_filter = self.request.query_params.get("status")
            stage_filter = self.request.query_params.get("stage")
            issue_type_filter = self.request.query_params.get(
                "issue_type"
            )
            reporter_role_filter = self.request.query_params.get(
                "reporter_role"
            )
            worker_filter = self.request.query_params.get("worker")

            if status_filter:
                queryset = queryset.filter(status=status_filter)

            if stage_filter:
                queryset = queryset.filter(stage=stage_filter)

            if issue_type_filter:
                queryset = queryset.filter(
                    issue_type_id=issue_type_filter
                )

            if reporter_role_filter:
                queryset = queryset.filter(
                    reporter_role=reporter_role_filter
                )

            if worker_filter:
                queryset = queryset.filter(
                    worker_id=worker_filter
                )

            if schedule_filter:
                queryset = queryset.filter(
                    schedule_id=schedule_filter
                )

            search = (
                self.request.query_params.get("search") or ""
            ).strip()

            if search:
                queryset = queryset.filter(
                    Q(code__icontains=search)
                    | Q(booking__booking_code__icontains=search)
                    | Q(reporter__username__icontains=search)
                    | Q(reporter__first_name__icontains=search)
                    | Q(reporter__last_name__icontains=search)
                )

            return queryset

        # Khách/nhân viên chỉ thấy khiếu nại CHÍNH MÌNH đã gửi,
        # không thấy khiếu nại người khác gửi về mình.
        queryset = queryset.filter(
            reporter=user
        )

        if schedule_filter:
            queryset = queryset.filter(
                schedule_id=schedule_filter
            )

        return queryset

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ComplaintCreateSerializer

        return ComplaintListSerializer

    def create(self, request, *args, **kwargs):
        files = request.FILES.getlist("attachments")

        if len(files) > MAX_ATTACHMENTS:
            return Response(
                {
                    "attachments": [
                        f"Tối đa {MAX_ATTACHMENTS} ảnh."
                    ]
                },
                status=400,
            )

        for f in files:
            if f.content_type not in ALLOWED_IMAGE_TYPES:
                return Response(
                    {
                        "attachments": [
                            f"File {f.name} không đúng định dạng ảnh."
                        ]
                    },
                    status=400,
                )

            if f.size > MAX_IMAGE_SIZE:
                return Response(
                    {
                        "attachments": [
                            f"File {f.name} vượt quá 5MB."
                        ]
                    },
                    status=400,
                )

        with transaction.atomic():
            serializer = self.get_serializer(
                data=request.data
            )

            serializer.is_valid(
                raise_exception=True
            )

            complaint = serializer.save()

            # Báo realtime cho admin/sidebar/danh sách khiếu nại.
            push_complaint_changed(complaint.id)

            for f in files:
                ComplaintAttachment.objects.create(
                    complaint=complaint,
                    file=f,
                    file_type=f.content_type or "",
                )

        return Response(
            ComplaintDetailSerializer(
                complaint,
                context={"request": request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


@COMPLAINT_DETAIL_SCHEMA
class ComplaintDetailView(generics.RetrieveAPIView):
    serializer_class = ComplaintDetailSerializer

    permission_classes = [
        _AnyAuthenticatedRoleForList,
        IsComplaintOwnerOrAdmin,
    ]

    def get_queryset(self):
        return Complaint.objects.select_related(
            "reporter",
            "worker",
            "booking",
            "issue_type",
            "resolved_by",
            "schedule",
        ).prefetch_related("attachments")


@COMPLAINT_CANCEL_SCHEMA
class ComplaintCancelView(APIView):
    permission_classes = [
        IsCustomerOrWorkerRole,
    ]

    @transaction.atomic
    def post(self, request, pk):
        complaint = get_object_or_404(
            Complaint.objects.select_for_update(),
            pk=pk,
        )

        if complaint.reporter_id != request.user.id:
            return Response(
                {"detail": "Không có quyền."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if complaint.status != Complaint.Status.PENDING:
            return Response(
                {
                    "detail": (
                        "Chỉ hủy được khi khiếu nại "
                        "đang ở trạng thái chờ xử lý."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ComplaintCancelSerializer(
            data={},
            context={
                "complaint": complaint,
                "request": request,
            },
        )

        serializer.is_valid(
            raise_exception=True
        )

        serializer.save()

        # Báo realtime sau khi khiếu nại được hủy.
        push_complaint_changed(complaint.id)

        return Response(
            ComplaintDetailSerializer(
                complaint,
                context={"request": request},
            ).data
        )


@COMPLAINT_RESOLVE_SCHEMA
class ComplaintResolveView(APIView):
    permission_classes = [
        IsAdminRole,
    ]

    @transaction.atomic
    def post(self, request, pk):
        complaint = get_object_or_404(
            Complaint.objects.select_for_update(),
            pk=pk,
        )

        serializer = ComplaintResolveSerializer(
            complaint,
            data=request.data,
            context={
                "request": request,
            },
        )

        serializer.is_valid(
            raise_exception=True
        )

        serializer.save()

        # Báo realtime sau khi admin xử lý khiếu nại.
        push_complaint_changed(complaint.id)

        return Response(
            ComplaintDetailSerializer(
                complaint,
                context={"request": request},
            ).data
        )


class ComplaintPreviewView(APIView):
    """
    GET /api/admin/complaints/<pk>/preview/
    ?outcome=...&charge_worker=true|false
    """

    permission_classes = [
        IsAdminRole,
    ]

    def get(self, request, pk):
        complaint = get_object_or_404(
            Complaint.objects.select_related(
                "booking",
                "booking__customer",
                "schedule",
                "worker",
            ),
            pk=pk,
        )

        outcome = request.query_params.get(
            "outcome",
            "",
        )

        if outcome not in (
            Complaint.Outcome.REFUND_CUSTOMER,
            Complaint.Outcome.PAY_WORKER,
        ):
            raise ValidationError(
                {
                    "outcome": "Kết quả không hợp lệ."
                }
            )

        if (
            outcome == Complaint.Outcome.REFUND_CUSTOMER
            and complaint.reporter_role
            != Complaint.ReporterRole.CUSTOMER
        ):
            raise ValidationError(
                {
                    "outcome": (
                        "Chỉ hoàn tiền khách cho "
                        "khiếu nại của khách."
                    )
                }
            )

        if (
            outcome == Complaint.Outcome.PAY_WORKER
            and complaint.reporter_role
            != Complaint.ReporterRole.WORKER
        ):
            raise ValidationError(
                {
                    "outcome": (
                        "Chỉ trả tiền nhân viên cho "
                        "khiếu nại của nhân viên."
                    )
                }
            )

        charge_worker = (
            request.query_params
            .get("charge_worker", "true")
            .lower()
            != "false"
        )

        from . import resolution_service

        return Response(
            resolution_service.preview_outcome(
                complaint=complaint,
                outcome=outcome,
                charge_worker=charge_worker,
            )
        )


class ComplaintAttachmentUploadView(
    generics.CreateAPIView
):
    parser_classes = [
        MultiPartParser,
        FormParser,
    ]

    serializer_class = ComplaintAttachmentSerializer

    permission_classes = [
        IsCustomerOrWorkerRole,
    ]

    def perform_create(self, serializer):
        from rest_framework.exceptions import ValidationError

        complaint = get_object_or_404(
            Complaint,
            pk=self.kwargs["pk"],
            reporter=self.request.user,
        )

        if complaint.status not in (
            Complaint.Status.PENDING,
            Complaint.Status.IN_REVIEW,
        ):
            raise ValidationError(
                {
                    "detail": (
                        "Khiếu nại đã đóng, "
                        "không thể thêm ảnh."
                    )
                }
            )

        if complaint.attachments.count() >= MAX_ATTACHMENTS:
            raise ValidationError(
                {
                    "file": (
                        f"Tối đa {MAX_ATTACHMENTS} ảnh."
                    )
                }
            )

        f = serializer.validated_data.get("file")

        if f is not None:
            if f.content_type not in ALLOWED_IMAGE_TYPES:
                raise ValidationError(
                    {
                        "file": (
                            "Chỉ nhận ảnh jpeg, png, webp."
                        )
                    }
                )

            if f.size > MAX_IMAGE_SIZE:
                raise ValidationError(
                    {
                        "file": "Ảnh vượt quá 5MB."
                    }
                )

        serializer.save(
            complaint=complaint
        )