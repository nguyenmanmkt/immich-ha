# Source Immich HA

Hướng dẫn chính: [README](../README.md). Cài mới từ [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md); witness tùy chọn tại [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md).

## Cấu trúc

| Đường dẫn | Chức năng |
| --- | --- |
| `src/` | Worker, controller, cấp quyền và tác vụ HA |
| `web/` | Source UI; sinh `src/admin-ui.mjs` bằng `tools/embed-ui.py` |
| `node/` | Agent, PostgreSQL, bootstrap HTTPS, gateway/witness |
| `tools/` | CLI cài đặt, bảo trì và kiểm tra source |
| `bootstrap_cloudflare.py` | Tạo tài nguyên Cloudflare cho bộ cài mới |
| `streaming_setup.py`, `quorum_stage.py` | Payload PostgreSQL, PKI và etcd |
| `deployment.py`, `ssh_nodes.py` | Transport cho công cụ bảo trì cũ; không dùng khi init/join |
| `tests/` | Kiểm thử cục bộ, không cần production |
| `docs/reference/` | Thiết kế tham khảo, không triển khai |

## Kiểm tra

```bash
python -m pip install -r requirements-ssh.txt
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.mjs
python tools/embed-ui.py
python tools/prepare-github.py
```

Bộ cài cục bộ dùng `requirements-local.txt`. Bộ kiểm thử đầy đủ/công cụ bảo trì dùng `requirements-ssh.txt`.

Không chạy init/join, stage hoặc cutover để cập nhật bản đang hoạt động. Xem [MAINTENANCE.vi.md](MAINTENANCE.vi.md). Không đưa cấu hình thật, khóa và nội dung `generated/` vào repo.
