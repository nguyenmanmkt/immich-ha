# Giữ xem ảnh khi bàn giao: kết quả khảo sát, chưa triển khai

Mục tiêu: trong bàn giao chủ động, tạm ngừng upload/sửa/xóa nhưng cố gắng giữ việc duyệt thư viện và xem ảnh/video. Không coi giao diện đang xem được là bằng chứng hai database đã khớp; vẫn phải chốt giao dịch, WAL và file.

## Những gì đã xác minh

- Immich 3.4.1 đang cài gọi `sessionRepository.update()` trong `AuthService.validateSession()` khi phiên cần cập nhật thời gian/app version, và có cập nhật thời hạn PIN. Vì vậy request xem ảnh có thể phát sinh SQL ghi.
- PostgreSQL `default_transaction_read_only` điều khiển mặc định cho giao dịch mới; không phải khóa toàn bộ các kết nối/giao dịch đang chạy. `ALTER ROLE ... SET` chỉ thiết lập mặc định khi đăng nhập phiên mới. Đây không phải một công tắc có thể bảo đảm app đang chạy chuyển nguyên vẹn sang chỉ đọc.
- Immich đang dùng `pg_try_advisory_lock`, `pg_advisory_lock`, `pg_advisory_unlock` theo phiên khi khởi động/thao tác database. Không được mặc nhiên dùng PgBouncer transaction pooling cho bản này.
- Nginx nằm trước HTTP/API; nó không chặn được worker nội bộ ghi DB/file. WebSocket là kết nối nâng cấp riêng, không biến PostgreSQL hoặc app thành read-only.
- Chỉ cho GET/HEAD là chưa đủ để xác định API đọc: phải kiểm tra danh sách endpoint, các lệnh tìm kiếm dùng POST, và tác động phụ của GET. Phiên WebSocket đã mở không chịu việc lọc lại từng HTTP request.

## Phương án cần thử: API đã khởi động sẵn + Nginx + proxy kết nối DB

Tách một tiến trình Immich chỉ có API, không có microservices worker, trên cổng loopback riêng. Khởi động nó khi hệ thống vẫn khỏe và DB active còn writable; không chờ đến lúc promote mới khởi động. API này vẫn dùng cơ chế xác thực Immich và chỉ được gateway cho phục vụ các endpoint xem đã kiểm tra.

Đặt PgBouncer trước kết nối DB của tiến trình API này, dùng **session pooling** để giữ tương thích các tính năng theo phiên. Đây là ứng viên kiến trúc, chưa phải kết luận tương thích failover đã được kiểm thử. Không sửa instance đang phục vụ để thử.

Luồng đề xuất:

1. Làm nóng API, đồng bộ WAL/file và thực hiện phần kiểm tra dài khi máy cũ vẫn phục vụ.
2. Gateway khóa thao tác thay đổi dữ liệu; ngừng/drain worker có thể ghi DB/file. API phục vụ xem vẫn chạy.
3. Ở điểm chốt cuối, ngắt có kiểm soát kết nối tới DB cũ, chờ giao dịch hoàn tất hoặc rollback, lấy mốc WAL và đợi replica áp dụng. Không để kết nối cũ tiếp tục ghi sau mốc này.
4. Fencing DB cũ, promote đích, đổi backend PgBouncer sang primary mới rồi cho kết nối tiếp tục. Cập nhật phiên đăng nhập cũng ghi vào **primary mới**, không cho hai PostgreSQL cùng nhận ghi ở hai nhánh khác nhau.
5. Gateway tiếp tục phục vụ đọc qua API đã nóng. Khi app/worker máy đích sẵn sàng, chuyển truy cập và mở ghi trở lại.

Vẫn có cửa sổ đổi DB: một số request phải chờ hoặc retry. Không cam kết zero downtime. Với session pooling, `PAUSE` chờ client ngắt kết nối; pool của ứng dụng có thể giữ phiên lâu. Không được dùng `PAUSE` như thể nó chỉ đợi hết một giao dịch. `KILL`/kết nối lại có thể làm request lỗi: cần chứng minh Immich xử lý được và gateway chỉ retry những request đọc đã xác nhận an toàn, trước khi trả response header/body. Không retry upload hay lệnh thay đổi dữ liệu một cách mù quáng.

## Tiêu chí thử nghiệm bắt buộc trước khi triển khai

- Dùng cụm PostgreSQL và API thử riêng, không đổi vai trò production.
- Xem bằng phiên cũ hơn một giờ và phiên vừa đăng nhập; kiểm tra album chia sẻ, khóa PIN và quyền xem của từng tài khoản. Không bỏ xác thực hoặc chia cache ảnh giữa người dùng.
- Kiểm tra API có sống khi kết nối DB bị cắt/đổi primary không: pool tự kết nối lại hay process bị thoát; đo lỗi HTTP, độ trễ và số request phải retry.
- Thử thumbnail, ảnh gốc, video Range, timeline, tìm kiếm và reconnect WebSocket. Theo dõi mọi ghi DB/file để bảo đảm writer cũ đã dừng trước khi promote.
- Chặn được upload/sửa/xóa và cả đường vào nội bộ, không chỉ UI public. Read API không được chạy worker nền có thể thay đổi file.
- Mất kết nối, proxy chết hoặc không xác nhận được giao dịch/LSN phải quay về luồng fencing hiện tại và báo trạng thái chính xác; không promote dựa trên bộ đếm thời gian đơn thuần.

## Nguồn

- [PostgreSQL: client connection defaults](https://www.postgresql.org/docs/16/runtime-config-client.html)
- [PostgreSQL: ALTER ROLE](https://www.postgresql.org/docs/16/sql-alterrole.html)
- [PgBouncer: pooling feature compatibility](https://www.pgbouncer.org/features.html)
- [PgBouncer: PAUSE, KILL, RESUME](https://www.pgbouncer.org/usage.html)
- [Nginx: WebSocket proxying](https://nginx.org/en/docs/http/websocket.html)
