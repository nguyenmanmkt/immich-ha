# Vận hành dashboard

Đăng nhập domain quản lý bằng email Cloudflare Access đã chọn. Cài mới: [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md).

## Điều khiển

| Mục | Chức năng |
| --- | --- |
| Thẻ node | Vai trò, dung lượng, CPU/RAM, số ảnh/video, WAL, file sync và heartbeat |
| ⚙ node | Bảo trì; standby có Đồng bộ lại dự phòng và Chuyển active |
| Tự đồng bộ dự phòng | Chuẩn bị lại máy còn lại từ active |
| Tự trở về máy ưu tiên | Chọn máy ưu tiên và cho phép bàn giao về khi đủ điều kiện |
| Slack | Bật/tắt alert, lưu webhook và gửi thử |
| Funnel | Theo dõi URL chung; ⚙ xem URL/IP; công tắc chỉ đổi listener chung |
| Backup | Liệt kê SQL backup trên từng node, lọc bản thiếu/cần kiểm tra |
| Nâng cấp | Chuẩn bị và áp dụng recipe Immich được hỗ trợ |
| Lịch sử | Tra cứu thao tác; thẻ tiến trình cuối trang ưu tiên job đang chạy |

Tự chuyển khi active gặp sự cố được bật trong vận hành bình thường. Nó khác công tắc trở về máy ưu tiên; bảo trì/nâng cấp có thể tạm khóa automation.

## Chuyển máy và bảo trì

Chỉ chuyển khi standby đã sẵn sàng. Job kiểm tra WAL/file, chốt ghi, fencing, cấp quyền mới rồi khởi động Immich đích; vẫn có gián đoạn.

Máy cũ trở lại nhận dữ liệu ngược trước khi làm dự phòng. Trở về máy ưu tiên cần khoảng 2 phút ổn định rồi mới bắt đầu bàn giao.

“Đang đưa vào bảo trì” chưa phải đã dừng dịch vụ. Chờ node xác nhận dừng app và hai folder Syncthing trước khi tắt/nâng cấp máy.

## Đọc trạng thái

- Số ảnh/video lấy từ database từng máy; cập nhật khoảng một phút, có thể hiển thị lệch tạm thời.
- CPU/RAM là số liệu LXC, không phải toàn Proxmox host. Dữ liệu cũ được đánh dấu.
- Syncthing và WAL là hai phép kiểm tra riêng. Standby không chạy API Immich trong streaming mode.
- Direct/relay đều có thể là kết nối Tailscale hợp lệ. LAN mode không yêu cầu Tailscale.
- Funnel được probe qua Internet mỗi phút, so node/epoch với active; quá 3 phút là số liệu cũ. Độ trễ HTTP khác ping Tailscale.
- Công tắc Funnel chờ witness xác nhận revision/trạng thái. Mất Cloudflare giữ listener đã áp dụng; dashboard có thể không truy cập được.

## Log

Trên từng **LXC Immich**:

```bash
systemctl status immich-ha-agent immich-ha-app --no-pager
journalctl -u immich-ha-agent -u immich-ha-app -n 80 --no-pager
```

Trên **witness**:

```bash
systemctl status immich-ha-quorum immich-ha-router --no-pager
journalctl -u immich-ha-quorum -u immich-ha-router -n 80 --no-pager
```

Bộ cài mới dùng `immich-ha-bootstrap.service` cho tác vụ cục bộ và công tắc Funnel; bản cài cũ dùng `immich-ha-funnel-control.timer`. Không dừng quorum để thử lỗi proxy/UI.

## Backup

SQL backup chỉ chứa database; cần media tương ứng để restore. Tên file giống nhau chưa chứng minh checksum bằng nhau hoặc restore thành công. Lịch SQL gốc do Immich quản lý; archive vật lý, manifest WAL và versioning có vòng đời riêng. Không có chính sách dọn chung cho mọi loại backup.

Lỗi cần xử lý: [MAINTENANCE.vi.md](MAINTENANCE.vi.md). Nâng phiên bản: [IMMICH-UPGRADE.vi.md](IMMICH-UPGRADE.vi.md).
