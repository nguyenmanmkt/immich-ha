# Immich HA

HA cho hai Immich native trên Proxmox LXC. PostgreSQL streaming đồng bộ database; Syncthing đồng bộ ảnh/video; Cloudflare Worker điều phối và cung cấp dashboard.

- Một node active được ghi; node dự phòng chạy PostgreSQL replica và dừng Immich.
- Tự chuyển khi sự cố, đồng bộ ngược, bàn giao và trở về máy ưu tiên.
- Dashboard theo dõi dịch vụ, backup, nâng cấp và thông báo Slack.
- Witness/quorum và Funnel là phần mở rộng tùy chọn khi cần dự phòng Cloudflare.

## Bắt đầu

1. [Cài hai LXC Immich và gắn ổ](cloudflare-ha/DEPLOYMENT.vi.md).
2. [Chuẩn bị Cloudflare và API token](cloudflare-ha/CLOUDFLARE-SETUP.vi.md).
3. Chạy `bash install.sh init` trên A, `bash install.sh join` trên B.
4. Đăng nhập dashboard, kiểm tra xem/upload và chuyển active.
5. Nếu cần, [ghép witness và bật Funnel](cloudflare-ha/WITNESS-SETUP.vi.md).

Bộ cài chạy cục bộ qua HTTPS, không cần SSH giữa các node. Dùng LAN hoặc Tailscale; riêng Funnel cần Tailscale trên máy xuất bản URL.

**Tình trạng bộ cài:** đã có kiểm thử tự động; chưa nghiệm thu end-to-end trên LXC mới. Không chạy init/join lên HA đang hoạt động.

## Mô hình

```text
App → Cloudflare Worker → Tunnel → gate của node active → Immich
A ↔ B: PostgreSQL WAL + Syncthing

Tùy chọn:
A + B + witness: quorum mTLS, đa số 2/3
App → Funnel chung → gateway witness → node có grant
```

`primary`/`backup` là ID cố định của A/B, không phải vai trò cố định. Cả hai đều có thể làm active.

## Tài liệu

| Nội dung | Hướng dẫn |
| --- | --- |
| Cài mới từ đầu | [DEPLOYMENT.vi.md](cloudflare-ha/DEPLOYMENT.vi.md) |
| Điều kiện và xử lý lỗi init/join | [INSTALL.vi.md](cloudflare-ha/INSTALL.vi.md) |
| Cấu hình, domain và IP | [CONFIG.vi.md](cloudflare-ha/CONFIG.vi.md) |
| Cơ chế HA | [ARCHITECTURE.vi.md](cloudflare-ha/ARCHITECTURE.vi.md) |
| Vận hành dashboard | [DASHBOARD.vi.md](cloudflare-ha/DASHBOARD.vi.md) |
| PostgreSQL / quorum | [STREAMING.vi.md](cloudflare-ha/STREAMING.vi.md), [QUORUM.vi.md](cloudflare-ha/QUORUM.vi.md) |
| Nâng cấp Immich | [IMMICH-UPGRADE.vi.md](cloudflare-ha/IMMICH-UPGRADE.vi.md) |
| Cập nhật bản đang chạy | [MAINTENANCE.vi.md](cloudflare-ha/MAINTENANCE.vi.md) |
| Kiểm tra và push source | [GITHUB.vi.md](GITHUB.vi.md) |

## Giới hạn

- Có gián đoạn khi bàn giao/promote/khởi động ứng dụng; không cam kết zero downtime.
- WAL và file đồng bộ bất đồng bộ; sự cố đột ngột có thể làm thiếu thay đổi mới nhất.
- Replication không thay backup lịch sử. Khôi phục database cần media tương ứng.
- Mất đa số quorum không cấp quyền mới; mất witness cũng mất gateway/Funnel chung.

## Phát triển

Python 3.12+, Node.js 24. Trong `cloudflare-ha/`:

```bash
python -m pip install -r requirements-ssh.txt
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.mjs
python tools/embed-ui.py
python tools/prepare-github.py
```

Preview: `node preview-server.mjs` → `http://127.0.0.1:8788`, dùng dữ liệu mô phỏng. Sửa `web/`, không sửa tay `src/admin-ui.mjs`. Quy ước source: [AGENTS.md](AGENTS.md).

Không commit `.env`, `project.json`, `.install/`, `generated/`, khóa hoặc backup. Giữ các file riêng để cập nhật đúng deployment. Giấy phép và nguồn tài sản: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
