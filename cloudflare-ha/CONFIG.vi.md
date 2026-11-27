# Cấu hình

Bộ cài init/join tự tạo cấu hình; không nhập biến SSH. Cài đặt: [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md).

## File riêng

| File | Nội dung |
| --- | --- |
| `project.json` | Domain, nhãn/IP node và Cloudflare Access |
| `.env` | Cloudflare Account ID/API token; credential bảo trì nếu có |
| `generated/` | Khóa, định danh deployment và report của tooling |
| `.install/node.json` | Credential client bootstrap cục bộ |
| `.install/witness.json` | Topology và authority đã pin khi ghép witness |
| `.install/witness-pki/` trên A | CA/private key của quorum mới |

Giữ các file này riêng, không commit hoặc sinh lại để cập nhật hệ thống.

## Trường project

| Trường | Ý nghĩa |
| --- | --- |
| `workerName` | Tên Worker; giữ nguyên khi cập nhật |
| `publicHost`, `adminHost` | Domain Immich và dashboard |
| `origins.primary`, `origins.backup` | HTTPS origin Tunnel → Nginx 8080 |
| `nodes.*` | Nhãn, IP, container ID và metadata node |
| `access.teamDomain`, `access.audience`, `access.emails` | Team domain, AUD và email quản trị |

Wizard lấy Team domain/AUD sau khi tạo Access. Dashboard dùng email OTP; origin dùng Service Auth, không dùng trang đăng nhập tương tác.

## Mạng

- Streaming: `network_mode: lan|tailscale` trong `/etc/immich-ha/streaming.json`.
- Quorum/router: `networkMode: lan|tailscale`. Thiếu trường vẫn giữ mặc định Tailscale cho bản cài cũ.
- LAN dùng IPv4 RFC1918; tailnet dùng `100.64.0.0/10`. Quorum luôn yêu cầu mTLS.
- IP hiển thị, IP replication, địa chỉ Syncthing và IP SSH là các cấu hình riêng.

Đổi IP peer cần cập nhật PostgreSQL bind/HBA/passfile, Syncthing và gateway. Đổi IP quorum còn cần cập nhật peer/client URLs và SAN chứng thư. Không xóa topology/data để stage lại.

## Đổi domain

1. Sửa `project.json`; giữ nguyên Worker name, node ID và khóa.
2. Cập nhật Custom Domain, hostname Tunnel và Access tương ứng trên Cloudflare.
3. Chạy `python deploy.py --update` từ checkout đang giữ cấu hình thật.
4. Cập nhật URL hiển thị trong config node nếu cần, restart agent và kiểm tra đăng nhập/upload.

Chỉ đổi domain không cần đổi `controller_url` workers.dev hoặc Durable Object storage ID. `deploy.py --update` không tự sửa DNS/Tunnel/Access.

## Funnel

Luồng mới dùng `funnel-enable` để kiểm tra gateway rồi báo URL/IP vào state Worker. Witness nhận công tắc qua credential riêng; không cần redeploy Worker khi bật/tắt.

Bản cài cũ dùng `funnelUrl`, `witnessTailscaleIp`, `nodes.*.funnelUrl`, `nodes.*.tailscaleIp` trong project và token `FUNNEL_CONTROL_TOKEN`. Token chỉ điều khiển listener chung, không cấp quyền active.

## Công cụ bảo trì cũ

`SSH_PRIMARY_*`, `SSH_BACKUP_*`, `SSH_COORDINATOR_*` và `SSH_JUMP_*` chỉ dùng cho tooling SSH; xem `.env.example` và [MAINTENANCE.vi.md](MAINTENANCE.vi.md). Host key phải được xác minh/pin. Không chép private key giữa các LXC.
