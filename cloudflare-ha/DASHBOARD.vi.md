# Vận hành dashboard và agent v2

## Thành phần và đường đi

```text
App / trình duyệt -> publicHost -> Worker -> origin gate của máy active -> Immich
Dashboard -> Worker API -> Durable Object -> agent LXC nhận lệnh qua heartbeat
File media, danh mục WAL và SQL backup -> Syncthing giữa hai LXC
Database -> PostgreSQL streaming trực tiếp qua Tailscale
```

Ảnh/database Immich không được lưu trong Durable Object. Durable Object giữ vai trò, quyền phục vụ, tiến trình và cấu hình điều phối.

## Cài đặt lên môi trường mới

Luồng đầy đủ nằm trong [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md). Các bước dưới đây chỉ là tổng quan; không thay thế điểm kiểm tra và điều kiện của từng script.

1. Chuẩn bị hai Immich LXC tương thích, media mount cùng đường dẫn và Syncthing chia sẻ đúng hai folder.
2. Tạo project.json từ file mẫu, điền Access; tạo .env chứa thông tin Cloudflare.
3. Deploy Worker, giữ lại toàn bộ generated để duy trì định danh/khóa.
4. Copy bộ node và config đúng máy. Bootstrap bằng install-node.sh ở chế độ quan sát; đọc và áp dụng upgrade-v2.sh để cài runtime v2. Không dùng installer nếu máy đã có config.
5. Hai Tunnel độc lập tới gate Nginx cổng 8080. Worker Custom Domain dành cho publicHost và adminHost.
6. Kiểm tra nguồn active, mount, đường media trong database, chiều Syncthing và khả năng restore trên dự phòng trước khi bật auto.

upgrade-v2.sh yêu cầu auto tắt, không có job đang chạy và định danh/khóa khớp. Nó có thể restart Immich, giữ bản config cũ trong `/etc/immich-ha/upgrade-backups/`. Các script giả định layout community-script; không phù hợp trực tiếp Docker hoặc hệ điều hành khác.

## Các điều khiển trên dashboard

- **Tự tiếp quản**: cho phép chuyển máy sau sự cố được xác nhận, khi standby đủ điều kiện.
- **Tự đồng bộ dự phòng**: tự chuẩn bị máy còn lại, xác nhận WAL và file; tạo lại replica vật lý khi cần.
- **Máy ưu tiên**: công tắc bật/tắt tự trở về; nút ⚙ chọn máy. Ngôi sao trên thẻ đánh dấu cấu hình ưu tiên.
- **Slack**: công tắc bật/tắt; ⚙ lưu webhook, gửi thử hoặc xóa. Webhook không trả lại qua API. Alert máy mất/sẵn sàng được gộp khoảng 60 giây để giảm lặp.
- **⚙ trong thẻ máy**: chi tiết, Bảo trì; máy dự phòng có Đồng bộ lại dự phòng và Chuyển active. Máy active không có nút tự bàn giao cho chính nó.
- **Backup**: inventory các file SQL gốc Immich trên từng máy; bản có tên giống nhau chưa phải bằng chứng checksum bằng nhau. Thao tác quản lý gốc mở trong Immich.

## Đồng bộ và chuyển máy

Trong chế độ streaming, máy dự phòng không chạy API Immich vì PostgreSQL đang recovery/chỉ đọc. Agent dùng health PostgreSQL và mount để báo sẵn sàng; gate vẫn từ chối production nếu chưa có grant active. Standby API và nạp dump vào database ứng viên chỉ thuộc chế độ legacy trước khi nâng cấp.

Bàn giao streaming: chuẩn bị replica và chiều Syncthing → chốt WAL/danh mục file ban đầu → kiểm tra → dừng ghi/chốt WAL cuối → xác nhận đủ WAL/file → fencing/cấp quyền mới → promote/khởi động Immich → đưa máy cũ thành dự phòng. Đủ chứng cứ fast handover thì bỏ khoảng đệm 30 giây; không bỏ thời gian đồng bộ cuối/startup và không bảo đảm zero downtime.

Nếu đã bật streaming và nâng cấp bàn giao WAL (`handoverVersion: 2` trên cả hai máy), các bước dump/restore được thay bằng mốc WAL + danh mục file, xác nhận replay và kiểm tra đủ ảnh. Không tạo `.dump` trong job chuẩn bị, bàn giao hoặc đưa máy cũ về dự phòng. Bản sao vật lý chỉ được tạo khi phải xây lại replica. Xem [STREAMING.vi.md](STREAMING.vi.md).

Sau sự cố, máy cũ quay lại không tự nhận ghi. Cần nhận dữ liệu từ owner hiện tại và xác nhận sẵn sàng. Nếu bật ưu tiên, sau 2 phút ổn định coordinator mới có thể bắt đầu bàn giao đầy đủ về máy ưu tiên.

Bảo trì cần phân biệt yêu cầu đang chờ với xác nhận đã dừng dịch vụ và hai folder Syncthing. Kết thúc bảo trì vẫn cần đồng bộ/kiểm tra lại trước khi nhận quyền active.

## Theo dõi và log

```bash
systemctl status immich-ha-agent immich-ha-app --no-pager
journalctl -u immich-ha-agent -u immich-ha-app -n 80 --no-pager
systemctl list-timers 'immich-ha-*'
```

Máy quản trị có thể chạy `python admin.py status --compact` và `python check-public.py`. Đây là kiểm tra thật và cần cấu hình/khóa; không chạy chúng trong CI công khai.

## Thống kê

- CPU là delta usage_usec cgroup chia theo quota/affinity của LXC. Mẫu đầu hiển thị Đang đo.
- RAM lấy MemTotal - MemAvailable do LXCFS cung cấp; là số liệu LXC, không phải cả host Proxmox.
- Immich đếm IMAGE/VIDEO chưa xóa, có archive, không tính visibility=hidden. Máy dự phòng đọc database replica cục bộ.
- Tailscale xác định peer theo thiết bị chia sẻ folder Syncthing, ping có timeout để phân biệt direct/relay. Không suy đoán từ DERP home region. Tham khảo: https://tailscale.com/docs/reference/ping-types
- Thống kê Immich/Tailscale chạy nền khoảng một phút; lỗi không được chặn heartbeat. Dữ liệu cũ được đánh dấu.
- Tốc độ Syncthing là trung bình kết nối giữa hai heartbeat, gồm cả folder database.

## Lưu trữ

Backup SQL gốc có lịch/retention do Immich quản lý và cần được xác nhận đã truyền sang peer. Archive vật lý, danh mục WAL và Syncthing versioning có vòng đời riêng, chưa có chính sách dọn chung; theo dõi dung lượng. Không đồng bộ thư mục data PostgreSQL đang ghi.

Với legacy dump còn bật, prune giữ 48 giờ theo giờ rồi một bản/ngày đến ngày 7. Khi tắt lịch dump, prune đi kèm cũng không chạy; file cũ phải được dọn có kiểm soát. Các tool cập nhật không tự xóa backup.

## Một thẻ tiến trình

Thẻ cuối trang ưu tiên job đang chạy cho đến khi kết thúc. Nếu không có job đang chạy, hiển thị sự cố/tiếp quản hiện tại hoặc kết quả gần nhất theo mốc bắt đầu sự kiện. Biến động readiness không được làm một takeover cũ chen lên job mới. Lịch sử giữ các thao tác trước đó.
