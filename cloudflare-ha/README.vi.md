# Immich HA — Worker và agent LXC

Source chính của [Immich HA](../README.md). Kiến trúc hiện tại dùng PostgreSQL streaming, Syncthing và một coordinator Cloudflare; owner có thể là một trong hai LXC.

## Chọn đúng luồng

| Việc cần làm | Tài liệu / công cụ |
| --- | --- |
| Triển khai mới | [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md), làm theo thứ tự từ đầu |
| Domain, IP, Access, SSH | [CONFIG.vi.md](CONFIG.vi.md) |
| Nâng cấp từ runtime v2 sang streaming | [STREAMING.vi.md](STREAMING.vi.md) |
| Cập nhật Worker/UI đã có | `python deploy.py --update` |
| Cập nhật runtime streaming + bàn giao WAL/nhanh | `python tools/upgrade-wal-handover.py` khi hai máy khỏe, không có job |
| Bật đồng bộ backup SQL gốc | `python tools/sync-native-backups.py` |
| Quan sát/vận hành | [DASHBOARD.vi.md](DASHBOARD.vi.md) |

`deploy.py --update` không cài agent lên LXC và không tạo DNS, Tunnel hoặc Access. `install-node.sh` chỉ dùng cho LXC chưa có agent. Không dùng lại bootstrap để cập nhật hệ thống đang chạy.

## Cấu trúc source

```text
web/                  UI và status model
src/                  Worker, controller, operations, fencing, thông báo
node/                 Agent, PostgreSQL adapter, launcher, Nginx, installer
tools/                CLI migration/rollout, build và kiểm tra source
deployment.py         SSH/control/upload dùng chung; jump host tùy chọn
streaming_setup.py    Logic stage PostgreSQL 16 và payload migration
tests/                Kiểm thử Python/JavaScript, không cần production
docs/reference/       Mẫu thiết kế, không phục vụ qua Worker
docs/research/        Khảo sát chưa triển khai
```

## Kiểm tra cục bộ

```bash
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.mjs
node preview-server.mjs
```

Python 3.12+, Node.js 24. Khi dùng SSH, cài `requirements-ssh.txt` trong venv. Sửa `web/` rồi chạy `python tools/embed-ui.py`; không sửa tay `src/admin-ui.mjs`.

## Bản riêng cho deployment

Tạo `.env` từ `.env.example`, `project.json` từ `project.example.json`. Giữ domain/IP/email ngoài code. `generated/` là dữ liệu riêng, đặc biệt khóa Worker/node và ZIP node. Không gửi chúng vào issue hoặc commit lên GitHub.

`python tools/prepare-github.py --zip` tạo `generated/github-source.zip` chỉ chứa source đã qua audit.
