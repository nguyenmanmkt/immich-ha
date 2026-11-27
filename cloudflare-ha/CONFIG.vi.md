# Cấu hình triển khai

Tạo `project.json` từ `project.example.json`; tạo `.env` từ `.env.example`. Không commit hai file riêng này.

| Trường project.json | Ý nghĩa |
| --- | --- |
| workerName | Tên Worker; giữ nguyên khi cập nhật hệ thống đã chạy |
| publicHost / adminHost | Domain Immich / dashboard HA |
| origins.primary / origins.backup | URL HTTPS của hai Tunnel tới gate cổng 8080 |
| nodes.*.label / host / ip / containerId | Metadata nhận diện hai máy |
| funnelUrl | URL HTTPS `.ts.net` chung; để trống để ẩn monitor |
| witnessTailscaleIp | IP Tailscale IPv4 witness, hiển thị trong ⚙ Funnel |
| nodes.*.funnelUrl | URL Funnel riêng của từng node, chỉ phục vụ khi node active |
| nodes.*.tailscaleIp | IP Tailscale IPv4 node, metadata cho dashboard |
| access.teamDomain | Team domain dạng example.cloudflareaccess.com |
| access.audience | Application AUD của Access bảo vệ dashboard |
| access.emails | Email quản trị được cho phép |

primary/backup là định danh cố định, không phải phân vai chính/phụ cố định.

## Thông tin riêng trong .env

- CLOUDFLARE_ACCOUNT_ID và CLOUDFLARE_API_TOKEN dùng triển khai.
- ORIGIN_ACCESS_CLIENT_ID / ORIGIN_ACCESS_CLIENT_SECRET dùng Service Auth phía origin nếu cần.
- SSH_PRIMARY_* / SSH_BACKUP_* dùng công cụ SSH, không phải mật khẩu Immich.
- SSH_JUMP_* là jump host dự phòng khi máy quản trị không tới thẳng LXC. Bỏ trống nếu không cần; thông tin đăng nhập jump host tách riêng với từng node. Xác minh host key của cả jump host và LXC trong `known_hosts`. Alias cấu hình host Asus từ deployment cũ vẫn được đọc để tương thích, nhưng source công khai không cần helper SSH cá nhân.
- SSH_COORDINATOR_* là SSH witness trên máy vật lý thứ ba. Các tool quorum có thể vào peer/witness qua node primary; dùng IP peer thật trong streaming, giữ host-key pin.
- SSH_KEY_PATH / SSH_KEY_PASSPHRASE là key chung được chỉ định rõ cho transport quorum khi chưa có key riêng. Nó không tự thay mật khẩu/khóa từng node trong mọi công cụ khác.

Giá trị .env được đọc literal, không nội suy shell. Không gửi file này vào chat hoặc issue.

Dashboard dùng Access đăng nhập email. Hai origin có thể dùng application Service Auth riêng với policy Include Service Token. Không dùng trang đăng nhập tương tác cho request Worker tới origin. Thiếu Team domain/AUD sẽ khóa API quản trị.

## Khi đổi domain hoặc IP

1. Sửa project.json.
2. Cập nhật Worker Custom Domain cho publicHost/adminHost, hostname Tunnel cho origins và Access tương ứng. deploy.py không tự sửa các thành phần này.
3. Chạy `python deploy.py --update` để cập nhật code/biến.
4. Cập nhật metadata trong `/etc/immich-ha/config.json` trên đúng máy nếu cần và restart agent. Giữ nguyên node, controller_url, token và generated khi chỉ đổi domain.
5. Nếu đổi publicHost, cập nhật URL trên app Immich.

Không đổi storage identifier trong src/worker.mjs trên hệ thống đang chạy: việc đó sẽ mở trạng thái coordinator khác. Không đổi workerName chỉ để đổi domain.

### Địa chỉ Funnel và quorum

URL Funnel chỉ nhận HTTPS hostname `.ts.net`, không có path/query/token; port được hỗ trợ là 443/8443/10000. IP metadata Tailscale dùng IPv4 trong `100.64.0.0/10`. Các giá trị này để hiển thị, không tự cấu hình mạng, đổi replication peer hoặc sửa ACL.

`tools/install-funnel-control.py --deploy` lấy địa chỉ đã xác nhận từ `generated/quorum-topology.json`, bổ sung metadata vào project, thêm token chuyên dụng, cập nhật Worker và cài timer witness. Có backup riêng trước khi sửa file. Tool chỉ tiếp tục khi HA khỏe/idle, Funnel chung đang bật và cấu hình witness cũ khớp; không dùng nó để đổi hostname/port bất kỳ bằng cách ghi đè.

Token `FUNNEL_CONTROL_TOKEN` được sinh và giữ trong bộ secret riêng, không điền vào project hoặc gửi cho browser. Nó chỉ dùng endpoint báo trạng thái/nhận yêu cầu Funnel; không dùng được như admin/agent token để cấp quyền Immich. Witness đọc cấu hình root-only `/etc/immich-ha/witness-funnel-control.json`. Các token cũ, cluster ID và khóa CA không bị xoay khi cài thêm tính năng.

Topology quorum là trạng thái triển khai riêng, chứa cluster ID/member IP và cờ checkpoint. Đổi LAN IP không tự đổi IP tailnet. Nếu đổi IP tailnet của thành viên, phải có kế hoạch sửa etcd peer/client URLs, SAN chứng thư, replication và gateway target; không xóa topology rồi chạy stage để “cập nhật IP”.

## Syncthing qua Tailscale

Địa chỉ remote device có thể dùng `tcp://100.x.y.z:22000` theo IP tailnet thật của máy kia. Device ID/folder ID giữ nguyên; không đổi thành đồng bộ hai chiều khi chỉ thay đường mạng. Kiểm tra direct/relay bằng `tailscale ping`.

IP hiển thị, IP SSH và địa chỉ peer Syncthing là các cấu hình độc lập. Máy quản trị chỉ SSH qua tailnet nếu chính nó được kết nối và ACL cho phép. Host key vẫn phải được xác minh/pin.

IP peer PostgreSQL streaming nằm trong `/etc/immich-ha/streaming.json` và passfile PostgreSQL trên từng LXC, được sinh từ IP `tailscale ip -4` khi stage. Đổi LAN IP không tự đổi chúng; nếu thay IP tailnet thật thì cần cập nhật cả cấu hình streaming, passfile và `pg_hba.conf` đúng peer. Đổi metadata trong `project.json` một mình không thay đường replication.

## Wrangler

`python tools/render-wrangler.py` sinh wrangler.jsonc từ project.json. File này riêng cho deployment, không commit. Cách deploy Python là luồng chính; nếu dùng Wrangler, cần tự cấu hình secret/binding tương ứng trước.
