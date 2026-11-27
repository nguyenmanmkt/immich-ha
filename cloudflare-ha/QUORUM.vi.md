# Quorum và điều phối dự phòng

Cài witness mới: [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md). Tài liệu này mô tả giao thức đang dùng, không phải chuỗi lệnh cài lại.

## Phạm vi

- Ba thành viên etcd: A, B và witness trên host độc lập; đa số 2/3.
- Cloudflare giữ điều phối chính. Fallback chạy trong agent A/B, không phải một tiến trình promote trên witness.
- Witness gateway chỉ đọc owner; Funnel chỉ là đường vào HTTPS.
- Đây là quorum của Immich HA, khác QDevice của Proxmox.

## Khóa chung

Etcd 3.6.17 dùng mTLS, tách CA peer/client, bind IP LAN hoặc tailnet đã chọn tại 2379/2380. Không public hai cổng này.

Generation được lưu bền vững; active key có TTL native. Cloudflare và fallback dùng cùng namespace `/immich-ha/<cluster>/control/admission/`, nên không được cấp hai writer đối nghịch khi khóa còn hiệu lực.

Agent kiểm tra grant đúng cluster, epoch, session, boot ID và thời hạn BOOTTIME trước khi promote/start/mở gate. Marker `quorum-required` buộc app startup tuân thủ khóa; không xóa marker hoặc nới quyền private key để sửa lỗi.

## Mất Cloudflare

1. Sau grace 15 giây, writer đang khỏe có thể chuyển sang authority fallback, giữ owner/epoch/session và native lease.
2. Lease fallback có TTL 180 giây, quyền cục bộ trừ margin 30 giây.
3. Máy khác chỉ nhận quyền sau khi lease cũ mất, nguồn tiếp tục vắng 300 giây, policy cho phép và replica đạt seed/WAL/media checks.
4. Mất đa số hoặc không xác nhận được dữ liệu thì không cấp quyền mới. Thời gian phục hồi không phải một SLA cố định.

Bảo trì, trạng thái frozen, job/nâng cấp và tombstone đã nhả khóa vẫn có hiệu lực. Fallback không tự rebuild replica hoặc tiếp tục job Cloudflare cũ.

## Cloudflare hồi phục

Agent đọc record tuyến tính và báo writer thực tế. Cloudflare tạm dừng thay đổi, gửi nonce; agent CAS nonce vào cùng lease rồi Cloudflare nhận đúng owner/epoch/session. Report cũ không được kéo hệ thống về writer hoặc epoch cũ. Automation chỉ chạy lại sau xác nhận grant mới.

## Gateway và Funnel

Router witness đọc quorum, cache tối đa 0,5 giây và trả 503 khi route hết quyền/không xác nhận được. Không dò ping để chọn owner và không replay POST/upload sang node khác.

Công tắc Funnel chỉ sửa listener chung đã quản lý. Bản init/join nhận lệnh qua client HTTPS; bản live cũ dùng timer/token chuyên dụng. Mất Cloudflare giữ listener hiện tại; cả hai cơ chế đều không cấp quyền ghi Immich.

## Kiểm chứng và giới hạn

Cần thử riêng: mất một thành viên, mất Cloudflare, đổi active, khôi phục node và mất đa số. Unit/CAS tests không thay thử nghiệm ứng dụng thật. Chặn heartbeat có kiểm soát không phải outage toàn bộ Cloudflare.

Quorum không phải STONITH phần cứng; fencing vẫn phụ thuộc kernel/systemd thực thi. Kết nối mTLS/agent keys thuộc cùng miền tin cậy, không phải giao thức chịu được root độc hại.

Log/vận hành: [DASHBOARD.vi.md](DASHBOARD.vi.md). Cập nhật bản live cũ: [MAINTENANCE.vi.md](MAINTENANCE.vi.md). Giữ cluster ID, CA và data etcd; không restage để sửa IP hoặc lỗi rollout.
