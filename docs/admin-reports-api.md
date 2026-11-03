# API Tổng quan & Báo cáo

Các endpoint GET yêu cầu JWT của ADMIN hoặc superuser.

| Endpoint | Nội dung |
| --- | --- |
| `/api/admin/reports/` | Kỳ, thẻ tổng hợp, timeline chart, thống kê dịch vụ, top 10 nhân viên, 10 đơn gần đây |
| `/api/admin/reports/workers/` | Bảng xếp hạng nhân viên có phân trang |
| `/api/admin/reports/services/` | Bảng thống kê dịch vụ có phân trang |
| `/api/admin/reports/revenue/` | Chi tiết từng dòng worker_earnings, có phân trang |
| `/api/admin/reports/bookings/` | Danh sách đơn trong kỳ, có nhân viên phụ trách và phân trang |
| `/api/admin/reports/export/` | File XLSX gồm Tổng hợp, Theo thời gian, Nhân viên, Dịch vụ, Chi tiết doanh thu |

## Tham số chung

- `period`: `all` (mặc định), `week`, `month`, `quarter`, `custom`.
- `date`: ngày nằm trong tuần/tháng/quý cần xem; mặc định hôm nay giờ Việt Nam.
- `start`, `end`: bắt buộc khi custom, định dạng YYYY-MM-DD. End là mốc không bao gồm.
- `group_by`: `auto` (mặc định), `day`, `week`, `month`.
  Auto: tối đa 31 ngày → day; tối đa 180 ngày → week; dài hơn → month.
- `sort`: `orders` (mặc định), `sessions`, `rating`, `commission`, `favorites`, `complaints`; áp dụng cho xếp hạng.
- `page`: mặc định 1; `page_size`: mặc định 20, tối đa 100.

Ngày yêu cầu nằm trong năm 1900–2100. Khoảng custom tối đa 3660 ngày.
Nhóm day tối đa 366 ngày; nhóm week tối đa 3660 ngày.
Tuần từ thứ Hai đến hết Chủ nhật. Quý là quý lịch. All từ dữ liệu cũ nhất đến hết hôm nay.
Ngày không có dữ liệu vẫn có bucket 0; bucket đầu/cuối được cắt đúng phạm vi kỳ.

Ví dụ:

```http
GET /api/admin/reports/?period=all
GET /api/admin/reports/?period=month&date=2026-10-01
GET /api/admin/reports/?period=quarter&date=2026-09-01
GET /api/admin/reports/?period=custom&start=2026-09-01&end=2026-10-01&group_by=day
GET /api/admin/reports/workers/?period=month&date=2026-09-01&sort=rating&page=1&page_size=10
GET /api/admin/reports/export/?period=month&date=2026-09-01
```

API JSON giữ convention hiện tại: CustomJSONRenderer bọc response `{data: ...}`.
FE dùng unwrap hiện có để đọc payload (envelope `data.data`). Tiền trả dạng chuỗi thập phân hai chữ số.
Excel trả binary trực tiếp, không có envelope; header Content-Disposition có tên file.
Export lấy toàn bộ dòng trong kỳ, không bị giới hạn bởi page/page_size; tối đa 50.000 dòng thu nhập.
Các ô tiền/số lượng trong XLSX là số; tên/ghi chú là chuỗi literal, không thực thi công thức.

## Công thức và các trường chính

`period` ghi rõ các cơ sở thời gian:

- Đơn: `Booking.created_at`.
- Doanh thu: `WorkerEarning.completed_at`.
- Buổi hoàn thành: `BookingSchedule.actual_end`, status COMPLETED.
- Đánh giá: `Review.created_at`, chỉ is_visible=True.

Vì thế một đơn tạo ở tháng trước vẫn có thể phát sinh hoa hồng ở tháng này.

`summary`:

- `total_orders`: số đơn, mọi trạng thái.
- `total_order_value`: SUM bookings.total_amount, mọi trạng thái.
- `cancelled_failed_order_value`: giá trị đơn CANCELLED/FAILED; `valid_order_value`: phần còn lại.
- `order_statuses`: số lượng từng trạng thái; `processing_orders`: PENDING/ASSIGNED/IN_PROGRESS trong kỳ.
- `completed_service_value`: SUM worker_earnings.gross_amount.
- `cleanwise_revenue`: SUM worker_earnings.commission_amount, dùng tỷ lệ đã lưu từng dòng, không áp lại 10%.
- `worker_income`: SUM worker_earnings.worker_amount.
- `cash_commission_owed_in_period`: hoa hồng CASH trong kỳ, settled_at=NULL.
- `cash_commission_owed_current`: hoa hồng CASH chưa thu trên mọi kỳ, số liệu hiện tại.
- `active_workers_current`: user WORKER, is_active=True và WorkerProfile ACTIVE, số liệu hiện tại; không phải online.
- `orders_today`: số đơn tạo hôm nay giờ Việt Nam, không phụ thuộc kỳ đang xem.

Đối soát:

- `missing_earning_sessions`: buổi hoàn thành trong kỳ có actual_end nhưng chưa có worker_earnings.
- `completed_sessions_missing_actual_end`: buổi hoàn thành thiếu actual_end trên toàn hệ thống, không thể gán đúng kỳ.
- `commission_on_refunded_bookings`: hoa hồng đã ghi sổ trong kỳ nhưng đơn đang có payment_status=REFUNDED.

Doanh thu là **hoa hồng đã ghi sổ trước điều chỉnh hoàn tiền**. Hệ thống chưa có sổ đảo hoa hồng theo từng buổi;
API không suy đoán khoản phải trừ từ tổng tiền khách được hoàn. Trả cờ đối soát và `revenue_basis` để FE ghi nhãn đúng.
API không tự backfill hoặc sửa sổ thu nhập bị thiếu.

`timeline`: start/end, orders, order_value, cleanwise_revenue, completed_service_value.
Tổng timeline và các nhóm dịch vụ khớp summary tương ứng.

`workers`: worker_id, name, username, active_current, profile_status, rank,
completed_orders (COUNT DISTINCT mã đơn có buổi hoàn thành của nhân viên), completed_sessions,
average_rating (null nếu chưa đánh giá), review_count, cleanwise_revenue, worker_income.
Một đơn có nhiều nhân viên có thể được tính cho từng người; không cộng xếp hạng nhân viên để suy ra tổng số đơn hệ thống.
Thứ tự mặc định: số đơn giảm dần, số buổi giảm dần, số đánh giá giảm dần, ID tăng dần.

`services`: mọi dịch vụ, kể cả inactive để giữ lịch sử; số đơn, tỉ lệ đơn (làm tròn 2 số), số đơn hủy/thất bại,
số buổi hoàn thành, giá trị đơn, giá trị buổi, hoa hồng. Tỉ lệ đã làm tròn có thể không cộng đúng 100%.

Các endpoint bảng trả results/count/page/page_size/total_pages/has_next/has_previous và period.

## Dữ liệu mẫu

Chạy tại CleanWiseApp:

```powershell
.\myenv\Scripts\python.exe -X utf8 manage.py seed_admin_reports --date 2026-10-01 --settings=core.settings.dev
```

Tạo thêm đơn `CW-RPT-*`, payment, buổi làm, phân công, worker_earnings và review trên 96 ngày.
Dùng khách hàng/nhân viên/dịch vụ hiện có; tạo worker `cw_report_worker_{service_id}` nếu dịch vụ chưa có nhân viên phù hợp.
Tài khoản mới không có mật khẩu đăng nhập; chỉ dùng làm fixture báo cáo.
Không sửa đơn cũ, không cộng/trừ ví, không gửi thông báo hoặc gọi cổng thanh toán.
Các dòng worker_earnings là snapshot tài chính cho kiểm thử API báo cáo, không phải fixture đối soát giao dịch ví.
Chạy lại cùng ngày neo không tạo trùng. Không tự khôi phục tài khoản mẫu đã bị đổi trạng thái.

Đã tạo ngày 01/10/2026: 45 đơn, 42 dòng thu nhập và 42 đánh giá trên ba dịch vụ;
nhân viên user_id 3, 6, 28 và 64. Có tiền mặt chưa thu, điểm khác nhau, review ẩn,
đơn nhiều buổi, hoàn thành một phần, đơn hủy, thất bại, và đơn chờ thực hiện.

Dữ liệu mẫu lịch tuần trước đó (`CW-WK-*`) chưa có sổ thu nhập nên được báo ở missing_earning_sessions.

## Kiểm tra

```powershell
.\myenv\Scripts\python.exe -X utf8 manage.py test apps.bookings.test_reports apps.bookings.test_admin_api apps.bookings.test_admin_worker_schedule --settings=core.settings.test --noinput
```

Bao gồm công thức, phân biệt ngày tạo/hoàn thành, không cộng trùng đơn nhiều buổi, review ẩn,
giờ Việt Nam, tuần/tháng/quý/năm nhuận, ngày trống, phân trang, quyền truy cập, XLSX và seed chạy lại.
## Xếp hạng yêu thích và khiếu nại

API xếp hạng nhân viên hỗ trợ thêm `sort=favorites` và `sort=complaints`, sắp xếp giảm dần trước khi phân trang. Mỗi dòng có:

- `favorite_count_current`: số khách hàng khác nhau hiện đang lưu nhân viên trong danh sách yêu thích. Không lọc theo kỳ; khách bỏ yêu thích thì số này giảm.
- `complaint_count`: phản ánh do khách gửi trong kỳ, gắn với nhân viên và chưa bị hủy; gồm chờ xử lý, đang xem xét, đã xử lý và bị từ chối.
- `complaint_pending_count`: chờ xử lý và đang xem xét.
- `complaint_resolved_count`: đã xử lý, không tự khẳng định nhân viên có lỗi.
- `complaint_rejected_count`: bị từ chối.

Kỳ khiếu nại dùng `created_at` theo giờ Việt Nam, bao gồm ngày bắt đầu và không bao gồm ngày kết thúc như các API báo cáo hiện có. Không tính phản ánh do nhân viên gửi hoặc chưa gắn nhân viên. Dữ liệu được tổng hợp riêng để tránh nhân số do nối bảng buổi làm, đánh giá và yêu thích.

Dashboard hiển thị hai cột mới trong Báo cáo → Xếp hạng nhân viên và hai lựa chọn Được yêu thích nhất / Nhiều khiếu nại nhất. File Excel cũng có các cột tương ứng và mô tả cơ sở số liệu ở sheet Tổng hợp. API vẫn yêu cầu quyền quản trị như trước. Không cần migration mới.
