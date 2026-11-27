# Immich HA — Worker và agent LXC

Source chính của [Immich HA](../README.md). Cloudflare điều phối chính; PostgreSQL streaming/Syncthing truyền dữ liệu giữa hai LXC. Quorum ba thành viên bắt buộc xác nhận quyền và hỗ trợ fallback; gateway/Funnel witness cung cấp URL dự phòng chung. Owner có thể là một trong hai Immich node.

## Chọn đúng luồng

| Việc cần làm | Tài liệu / công cụ |
| --- | --- |
| Triển khai mới | [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md), làm theo thứ tự từ đầu |
| Hiểu kiến trúc và tình huống lỗi | [ARCHITECTURE.vi.md](ARCHITECTURE.vi.md) |
| Quorum, fallback và gateway/Funnel | [QUORUM.vi.md](QUORUM.vi.md) |
| Domain, IP, Access, SSH | [CONFIG.vi.md](CONFIG.vi.md) |
| Nâng cấp từ runtime v2 sang streaming | [STREAMING.vi.md](STREAMING.vi.md) |
| Cập nhật Worker/UI đã có | `python deploy.py --update` |
| Cập nhật runtime streaming + bàn giao WAL/nhanh | `python tools/upgrade-wal-handover.py` khi hai máy khỏe, không có job |
| Bật đồng bộ backup SQL gốc | `python tools/sync-native-backups.py` |
| Quan sát/vận hành | [DASHBOARD.vi.md](DASHBOARD.vi.md) |
| Cài giám sát/công tắc Funnel chung | `python tools/install-funnel-control.py --deploy` sau khi đã xác nhận Funnel chung/riêng |

`deploy.py --update` không cài agent lên LXC và không tạo DNS, Tunnel hoặc Access. `install-node.sh` chỉ dùng cho LXC chưa có agent. Không dùng lại bootstrap để cập nhật hệ thống đang chạy.

## Cấu trúc source

```text
web/                  UI và status model
src/                  Worker, controller, operations, fencing, thông báo
node/                 Agent, PostgreSQL adapter, launcher, Nginx, installer
tools/                CLI migration/rollout, build và kiểm tra source
deployment.py         SSH/control/upload dùng chung; jump host tùy chọn
streaming_setup.py    Logic stage PostgreSQL 16 và payload migration
quorum_setup.py       Topology riêng, SSH ba node, kiểm tra và quyền marker
quorum_stage.py       PKI/mTLS và cấu hình thành viên etcd
local_gateway.py      Kiểm tra/render gateway node
witness_gateway_setup.py  Cài selector và Nginx gateway witness
funnel_setup.py       Kiểm tra Serve/Funnel, giữ route không liên quan
tests/                Kiểm thử Python/JavaScript, không cần production
docs/reference/       Mẫu thiết kế, không phục vụ qua Worker
docs/research/        Khảo sát chưa triển khai
```

## Kiểm tra cục bộ

```bash
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.mjs
python tools/embed-ui.py
python tools/prepare-github.py
node preview-server.mjs
```

Python 3.12+, Node.js 24. Khi dùng SSH, cài `requirements-ssh.txt` trong venv. Sửa `web/` rồi chạy `python tools/embed-ui.py`; không sửa tay `src/admin-ui.mjs`.

## Bản riêng cho deployment

Tạo `.env` từ `.env.example`, `project.json` từ `project.example.json`. Giữ domain/IP/email ngoài code. `generated/` là dữ liệu riêng, đặc biệt khóa Worker/node và ZIP node. Không gửi chúng vào issue hoặc commit lên GitHub.

`python tools/prepare-github.py --zip` tạo `generated/github-source.zip` chỉ chứa source đã qua audit.

`src/funnel-health.mjs` chỉ quan sát URL chung; `src/funnel-control.mjs` lưu yêu cầu/xác nhận riêng, không điều phối writer. `node/witness_router.py` chỉ đọc quorum/định tuyến; `node/witness_funnel.py` chỉ điều khiển listener chung. Hai service này khác agent Immich và thành viên etcd.
