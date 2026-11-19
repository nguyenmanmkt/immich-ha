# Cấu hình triển khai

Tạo `project.json` từ `project.example.json`; tạo `.env` từ `.env.example`. Không commit hai file riêng này.

| Trường project.json | Ý nghĩa |
| --- | --- |
| workerName | Tên Worker; giữ nguyên khi cập nhật hệ thống đã chạy |
| publicHost / adminHost | Domain Immich / dashboard HA |
| origins.primary / origins.backup | URL HTTPS của hai Tunnel tới gate cổng 8080 |
| nodes.*.label / host / ip / containerId | Metadata nhận diện hai máy |
| access.teamDomain | Team domain dạng example.cloudflareaccess.com |
| access.audience | Application AUD của Access bảo vệ dashboard |
| access.emails | Email quản trị được cho phép |

primary/backup là định danh cố định, không phải phân vai chính/phụ cố định.

## Thông tin riêng trong .env

- CLOUDFLARE_ACCOUNT_ID và CLOUDFLARE_API_TOKEN dùng triển khai.
- ORIGIN_ACCESS_CLIENT_ID / ORIGIN_ACCESS_CLIENT_SECRET dùng Service Auth phía origin nếu cần.
- SSH_PRIMARY_* / SSH_BACKUP_* dùng công cụ SSH, không phải mật khẩu Immich.
- SSH_JUMP_* là jump host dự phòng khi máy quản trị không tới thẳng LXC. Bỏ trống nếu không cần; thông tin đăng nhập jump host tách riêng với từng node. Xác minh host key của cả jump host và LXC trong `known_hosts`. Alias cấu hình host Asus từ deployment cũ vẫn được đọc để tương thích, nhưng source công khai không cần helper SSH cá nhân.

Giá trị .env được đọc literal, không nội suy shell. Không gửi file này vào chat hoặc issue.

Dashboard dùng Access đăng nhập email. Hai origin có thể dùng application Service Auth riêng với policy Include Service Token. Không dùng trang đăng nhập tương tác cho request Worker tới origin. Thiếu Team domain/AUD sẽ khóa API quản trị.

## Khi đổi domain hoặc IP

1. Sửa project.json.
2. Cập nhật Worker Custom Domain cho publicHost/adminHost, hostname Tunnel cho origins và Access tương ứng. deploy.py không tự sửa các thành phần này.
3. Chạy `python deploy.py --update` để cập nhật code/biến.
4. Cập nhật metadata trong `/etc/immich-ha/config.json` trên đúng máy nếu cần và restart agent. Giữ nguyên node, controller_url, token và generated khi chỉ đổi domain.
5. Nếu đổi publicHost, cập nhật URL trên app Immich.

Không đổi storage identifier trong src/worker.mjs trên hệ thống đang chạy: việc đó sẽ mở trạng thái coordinator khác. Không đổi workerName chỉ để đổi domain.

## Syncthing qua Tailscale

Địa chỉ remote device có thể dùng `tcp://100.x.y.z:22000` theo IP tailnet thật của máy kia. Device ID/folder ID giữ nguyên; không đổi thành đồng bộ hai chiều khi chỉ thay đường mạng. Kiểm tra direct/relay bằng `tailscale ping`.

IP hiển thị, IP SSH và địa chỉ peer Syncthing là các cấu hình độc lập. Máy quản trị chỉ SSH qua tailnet nếu chính nó được kết nối và ACL cho phép. Host key vẫn phải được xác minh/pin.

IP peer PostgreSQL streaming nằm trong `/etc/immich-ha/streaming.json` và passfile PostgreSQL trên từng LXC, được sinh từ IP `tailscale ip -4` khi stage. Đổi LAN IP không tự đổi chúng; nếu thay IP tailnet thật thì cần cập nhật cả cấu hình streaming, passfile và `pg_hba.conf` đúng peer. Đổi metadata trong `project.json` một mình không thay đường replication.

## Wrangler

`python tools/render-wrangler.py` sinh wrangler.jsonc từ project.json. File này riêng cho deployment, không commit. Cách deploy Python là luồng chính; nếu dùng Wrangler, cần tự cấu hình secret/binding tương ứng trước.
