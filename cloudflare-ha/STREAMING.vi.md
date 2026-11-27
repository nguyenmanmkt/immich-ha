# PostgreSQL streaming

## Cấu hình hiện tại

| Thành phần | Giá trị |
| --- | --- |
| Cluster / port | PostgreSQL `16/ha`, `5433` |
| Database | `immich_stream` |
| Đồng bộ | Physical streaming bất đồng bộ qua LAN/Tailscale |
| Source | Một PostgreSQL writable duy nhất |
| Standby | Recovery, không chạy Immich |

Cluster `16/main` được giữ sau migration. Không tự trỏ ứng dụng về cluster cũ sau khi HA đã nhận ghi. Cài mới do [init/join](DEPLOYMENT.vi.md) thiết lập; không cần chạy stage/cutover riêng.

## Bàn giao và đồng bộ ngược

Nguồn chốt mốc WAL và danh mục file; đích xác nhận đúng lineage/timeline, replay đạt mốc và đủ media. Sau chốt cuối/fencing mới promote. Không dùng dump/restore trong job streaming.

Máy cũ được tạo lại replica bằng `pg_basebackup` khi cần; candidate được kiểm tra trước khi thay cụm dự phòng. PGDATA cũ giữ trong `/mnt/immich-storage/backups/pg-stream/`. Không sao chép PGDATA qua Syncthing, không tự xóa archive khi đang có job.

Manifest `wal-*.manifest.json.gz` trong `/backups/postgres` chỉ ghi mốc WAL và danh mục file, không chứa database. SQL backup gốc của Immich được đồng bộ riêng theo rule cho phép.

## Kết nối và dung lượng

PostgreSQL chờ địa chỉ mạng đã cấu hình trước khi start. LAN không yêu cầu Tailscale; tailnet mode chờ `tailscale0`. Nếu thiếu listener 5433, kiểm tra mạng trước khi restart có kế hoạch.

Không dùng replication slot cố định. WAL có thể bị loại bỏ khi peer offline lâu, nên cần base backup lại; seed mất kết nối quá 15 phút không được tự promote. Các cấu hình WAL không phải giới hạn cứng dung lượng ổ đĩa.

## Kiểm tra trong LXC

```bash
pg_lsclusters
ss -lnt 'sport = :5433'
runuser -u postgres -- psql -X -p 5433 -d immich_stream -c 'SELECT pg_is_in_recovery();'
```

Active trả `f`, standby trả `t`. Dashboard phân biệt nguồn, nhận WAL, đang replay và mất kết nối. “Đã đồng bộ” của Syncthing không chứng minh WAL đã khớp.

Streaming không thay backup: metadata và file là hai luồng bất đồng bộ. Giữ media + SQL backup độc lập và thử restore riêng. Cơ chế cấp quyền: [ARCHITECTURE.vi.md](ARCHITECTURE.vi.md).
