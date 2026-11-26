# Nâng cấp Immich trên hai LXC

Đây là nâng phiên bản ứng dụng, khác với deploy Worker hoặc cập nhật agent. Dashboard có mục **Nâng cấp** với hai lần xác nhận riêng. Không chạy `update` community-script trực tiếp trên cụm HA: updater gốc điều khiển service/database mặc định, khác với `immich-ha-app` và PostgreSQL `16/ha:5433`.

## Cài tính năng cho hệ thống đang chạy

Trên máy quản trị, dùng source và cấu hình riêng hiện tại:

```bash
cd cloudflare-ha
python tools/upgrade-immich-ha.py --via primary
python tools/upgrade-immich-ha.py --via primary --deploy
```

`--via` chọn node SSH đầu tiên; peer lấy từ cấu hình PostgreSQL streaming trên node đó và truy cập qua Tailscale bằng SSH tunnel. Hai máy vẫn dùng credentials riêng và host key đã xác minh. Không chép private key vào LXC. `--deploy` chỉ cập nhật module agent và Worker/UI, giữ owner/epoch, database, IP, token và dữ liệu; **không nâng Immich**. Khi kiểm tra thất bại, automation giữ OFF và ghi rõ trạng thái cần xử lý. Bản trước rollout nằm riêng trong `generated/` và `/etc/immich-ha/upgrade-feature-*`.

Cloudflare có thể giữ phiên bản code Durable Object cũ tối đa khoảng 5 phút theo chiến lược deferred mặc định. Tool chờ tối đa 7 phút để nhận capability mới; không reset state hay tạo namespace khác để thúc cập nhật. Xem [Durable Objects code updates](https://developers.cloudflare.com/durable-objects/deployments/durable-objects-code-updates/).

## Phiên bản được phép

Môi trường đầu tiên được hỗ trợ là Debian 13 amd64, LXC từ 6 GiB RAM trở lên. Native dependencies được giữ nguyên; checksum bundle và fingerprint Node.js được kiểm tra lại trước khi cài.

Recipe đầu tiên: **3.4.1 → 3.4.2**, Node.js 24, PostgreSQL 16, VectorChord 1.1.1, CPU ML và native layout `/opt/immich`. Code server/web/CLI build từ commit `b6b4b80df113656bfa4872cab31c7319e89438a9`, archive được ghim SHA-256. Diff đã đối chiếu không đổi schema, plugin-core hay code/dependencies ML; runtime ML/plugin hiện tại được kiểm tra và tái sử dụng. Nhãn package ML cũ được giữ vì không cài lại Python environment. OpenVINO, extension khác hoặc phiên bản khác bị chặn; không tự thay PostgreSQL major, apt packages hay chạy updater từ URL do người dùng nhập.

Recipe được rà soát và kiểm thử điều phối không đồng nghĩa đã thử nâng ứng dụng thật. Cần phân biệt kết quả unit/controller test, build staging thực tế và lần áp dụng/migration thực tế trong nhật ký triển khai. Thêm phiên bản mới phải cập nhật recipe Python/Worker, đọc release notes và kiểm chứng lại; không chỉ đổi số phiên bản.

## Vận hành trên dashboard

1. Chờ cả hai máy online, dữ liệu/WAL khớp và đủ dung lượng. Chọn phiên bản được hỗ trợ.
2. **Chuẩn bị**: kiểm tra role/database, build target trước rồi source, giữ code hiện tại và thư viện đang phục vụ. Job tạm ngưng chuyển máy tự động. Không chạy app mới trên replica.

Preflight thêm đúng rule `/backups/immich-upgrades` lên đầu ignore của folder `immich-library`, giữ nguyên các rule khác. Không mở thêm đường đồng bộ nào; source/build cache/backup nâng cấp ở riêng trên từng máy.
3. Khi **Đã chuẩn bị**, kiểm tra release notes. **Nâng cấp cả hai** yêu cầu nhập phiên bản để xác nhận; sẽ có thời gian gián đoạn.
4. Worker khóa bàn giao/tự tiếp quản/tự trở về. Active đóng ứng dụng, tạo backup cuối bằng `pg_dump -Fc -p 5433 -d immich_stream`, kiểm tra `pg_restore --list` và checksum. Backup này chỉ dành cho lần nâng cấp, không bật lại lịch dump HA định kỳ.
5. Xác minh checksum, cài code standby rồi active, giữ nguyên các thư mục code cũ. Chỉ active có lease đúng epoch mới được khởi động ứng dụng mới/migration. PostgreSQL streaming tiếp tục; replica không chạy Immich.

Giữa bước backup và cài code, hệ thống xuất thêm SQL.gz vào thư mục backup phẳng rồi chờ Syncthing chuyển đến máy dự phòng. Máy đó kiểm tra SHA-256, gzip CRC và giữ một bản riêng. Chưa có xác nhận backup trên **cả hai máy** thì chưa cài code. Bản dump gốc và record được fsync, retry không ghi đè bản trước nâng cấp. File SQL.gz pre-upgrade là backup bảo vệ lần nâng cấp này, không phải lịch SQL tự động của Immich; chỉ file SQL phẳng đi theo rule cho phép hiện có, thư mục source/build/private vẫn bị loại trừ.
6. Chờ active khỏe, xác nhận WAL + danh mục file, canPromote và phiên bản hai máy. Sau khi thành công mới mở phục vụ/khôi phục tự động theo tùy chọn đã lưu. Owner không thay đổi trong nâng cấp.

Chuẩn bị gắn với owner/epoch. Nếu bàn giao trước khi áp dụng, phải chuẩn bị lại. Trình duyệt đóng không hủy job: tiến trình và receipt ở Worker/agent. Build timeout 60 phút, có log riêng; source/candidate tách khỏi app và khỏi Syncthing. Cần ít nhất 2,3 GiB ổ hệ thống và 12 GiB media; quá trình build còn phụ thuộc RAM, mạng và toolchain đang cài.

## Khi có lỗi

- Lỗi chuẩn bị: code/database đang chạy chưa bị thay. Đọc `build.log`, sửa môi trường rồi tạo job chuẩn bị mới; staging lỗi được giữ để điều tra.
- Lỗi áp dụng: giữ khóa chuyển vai trò, auto OFF; không bấm Resume chung để mở code cũ. Dùng **Tiếp tục nâng cấp** sau khi sửa nguyên nhân. Job retry giữ stage/checksum và backup ban đầu, xác minh lại vai trò; không chạy shell tùy ý.
- Khi khởi động bản mới, agent xuất lease coordinator còn hạn vào file grant cho `app-run-v2` trước khi mở mode active và gọi systemd. Không tái sử dụng lease trước lúc dừng ghi; mutation lock của job không được khiến bước này phụ thuộc vòng activate bên ngoài.
- Sau reboot giữa chừng, Retry có thể mở lại đúng cụm HA đang dừng: đối chiếu system identifier và `standby.signal`; nguồn cần lease active đúng owner/epoch. Không promote hoặc ghi đè PGDATA. Nếu mất điện đúng giữa hai lần rename app, chỉ dùng candidate mới có checksum đúng và bản code cũ còn nguyên để tiếp tục; không chạy code cũ trên database đã nâng cấp.
- Không tự downgrade code hoặc restore database sau migration. Nếu cần khôi phục phiên bản cũ, phải lập kế hoạch riêng: dừng ghi cả hai bên, xác minh backup/ảnh tương ứng và khôi phục có kiểm soát. Backup SQL không chứa thư viện ảnh.
- Code trước nâng cấp: `/opt/immich/.ha-previous-<stageId>`. Candidate mới: `.ha-next-<stageId>`. Marker chặn tự khởi động active nằm trong runtime; không xóa marker để bỏ qua kiểm tra.
- Backup/checksum/source/build log nằm trong `/mnt/immich-storage/backups/immich-upgrades/<stageId>/`. Không đồng bộ thư mục này bằng Syncthing, không mở rộng ignore backup. Chưa tự xóa staging/code cũ: chỉ dọn khi đã xác nhận nâng cấp và giữ đủ backup độc lập.

Nguồn đối chiếu: [Immich upgrading](https://docs.immich.app/install/upgrading/), [release 3.4.2](https://github.com/immich-app/immich/releases/tag/v3.4.2), [community native build](https://github.com/community-scripts/ProxmoxVE/blob/main/ct/immich.sh). Native LXC là tích hợp community; pipeline này không dùng lệnh Docker Compose.
