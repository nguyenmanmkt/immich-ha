# PostgreSQL streaming replication

Chế độ tùy chọn này thay việc nạp dump theo giờ bằng PostgreSQL physical streaming replication. Ảnh/video vẫn đồng bộ bằng Syncthing. Backup SQL gốc Immich giữ vai trò khôi phục lịch sử; replication không thay thế backup. Lịch dump HA thuộc chế độ legacy và có thể tắt sau khi đã kiểm tra streaming/SQL backup.

Có thể tắt riêng dump HA định kỳ bằng marker `/etc/immich-ha/disable-scheduled-dump` và `systemctl disable --now immich-ha-dump.timer` trên cả hai máy. Installer và công cụ cutover tôn trọng marker này. Backup SQL gốc Immich giữ lịch riêng. Luồng bàn giao WAL mới không tạo hoặc restore dump; các file dump cũ không tự bị xóa khi tắt lịch, và lịch prune đi kèm dump định kỳ cũng ngừng chạy.

## Cấu trúc

- Cụm riêng `16/ha`, cổng `5433`, dữ liệu `/var/lib/postgresql/16/ha`; database `immich_stream`.
- Cụm `16/main` cũ ở cổng `5432` được giữ nguyên. Không xóa database cũ trong bước nâng cấp.
- Máy active chạy Immich đầy đủ worker. Máy dự phòng **không chạy Immich API** vì PostgreSQL đang chỉ đọc; agent kiểm tra PostgreSQL và ổ ảnh để báo sẵn sàng.
- Kết nối replication dùng IP Tailscale giữa hai LXC. PostgreSQL chỉ lắng nghe loopback và IP Tailscale; `pg_hba.conf` chỉ cho IP peer `/32` dùng tài khoản replication SCRAM. Không đưa PostgreSQL qua Tunnel hoặc mở cổng Internet.
- Cấu hình riêng `/etc/immich-ha/streaming.json`. Mật khẩu replication ở `/var/lib/postgresql/.immich-ha-replication.pass`, chỉ user `postgres` đọc. Môi trường app ở `/var/lib/immich-ha/runtime/streaming-app.env`, chỉ root/nhóm `immich` đọc. Không commit các file này.
- Không chia sẻ PGDATA hoặc file WAL bằng Syncthing.
- Có thể truyền riêng các backup SQL gốc Immich bằng `python tools/sync-native-backups.py`. Các file `immich-db-backup-*.sql.gz`/`.sql` và bản `uploaded-`/`pre-restore-` trong gốc `/backups` đi theo folder thư viện. Các thư mục `/backups/pg-stream`, `/backups/postgres`, `/backups/recovery` tiếp tục bị loại trừ khỏi folder này; folder `immich-database` riêng truyền danh mục bàn giao WAL và các dump cũ nếu còn.

## Nâng cấp một hệ thống v2 đang chạy

Yêu cầu PostgreSQL **16**, cùng phiên bản minor và cùng extension trên hai máy; Tailscale LXC hoạt động. Các công cụ hiện cố định cổng 5433, tên cluster `ha`, tên database `immich_stream`; không dùng cho cluster hiện có trùng tên. Cần tối thiểu 3 GiB trống trên ổ hệ thống để staging, và dung lượng lưu rollback trên ổ media.

Chạy từ thư mục `cloudflare-ha`, với thông tin SSH/Cloudflare đã có trong `.env` và `generated/`:

```bash
python tools/streaming-upgrade.py stage
python tools/streaming-stage-test.py
python deploy.py --update
python tools/streaming-cutover.py
```

`stage` tạo bản database riêng, dùng `pg_basebackup` và `pg_verifybackup`, rồi kiểm tra một commit xuất hiện ở máy dự phòng. Immich thật vẫn phục vụ bằng cụm cũ. Chạy một lần; không chạy lại stage/cutover khi cấu hình đã tồn tại hoặc đã enabled. `streaming-stage-test.py` chỉ cho phép cụm chưa enabled và thử promote/rejoin hai chiều; tuyệt đối không dùng nó làm công cụ failover production.

`cutover` kiểm tra owner/epoch không đổi, lưu file quay lui, tạm tắt tự tiếp quản/tự trở về/tự chuẩn bị dự phòng, dừng ghi Immich, sao chép database lần cuối, đợi WAL và đối chiếu số asset. Sau đó chuyển app và agent sang cụm mới; chỉ bật lại tự động sau khi cả hai máy được xác nhận sẵn sàng. Có gián đoạn trong bước này.

Nếu lỗi trước khi mở app mới, công cụ khôi phục các file v2 đã lưu. Nếu app mới đã có thể nhận ghi, công cụ **giữ streaming và để tự động OFF khi chưa xác nhận sức khỏe**. Không tự quay về database cũ vì có thể làm mất những thay đổi vừa nhận. Bản ghi cục bộ nằm trong `generated/streaming-*.json`, các file runtime trước nâng cấp trong `/etc/immich-ha/streaming-rollback/`.

## Chuyển vai trò và máy cũ trở lại

Hệ thống đã chạy streaming từ phiên bản trước có thể nâng cấp luồng bàn giao bằng `python tools/upgrade-wal-handover.py`. Công cụ yêu cầu hai máy đang khỏe và không có job/pending; nó lưu module/cấu hình tự động, tạm tắt tự động, cập nhật hai agent trước rồi Worker, và khôi phục cấu hình tự động khi đã xác nhận sẵn sàng. Không đổi owner/epoch. Cả hai agent phải báo `handoverVersion: 2` trước khi Worker cho tạo job WAL mới. Các action cũ chỉ được giữ để hoàn tất job đã có hoặc phục vụ chế độ dump legacy.

Trong vận hành bình thường, Cloudflare Worker/Durable Object cấp owner/epoch và lease. Khi đã bật quorum/fallback, agent còn phải lấy khóa chung; nếu mất điều phối CF, fallback chỉ cấp quyền theo native lease/quorum và đối chiếu khi CF hồi phục. Agent chỉ promote với grant active còn hạn, đúng epoch/lineage, seed thuộc nguồn cũ và không còn WAL đã nhận chờ áp dụng. Gate đóng khi hết quyền; reboot không tự mở writer. Xem [ARCHITECTURE.vi.md](ARCHITECTURE.vi.md) và [QUORUM.vi.md](QUORUM.vi.md).

Bàn giao có kế hoạch: chốt danh mục file và mốc WAL ban đầu, đồng bộ ảnh/WAL, kiểm tra nguồn và timeline. Sau đó dừng app nguồn, tạo danh mục/mốc WAL cuối, đợi đúng replica xác nhận replay rồi dừng PostgreSQL nguồn. Máy đích kiểm tra checksum danh mục, đủ file và replay đạt mốc cuối. Chỉ sau xác nhận đó và khi đủ điều kiện fencing, máy đích mới được promote. Không gọi `pg_dump` hoặc `pg_restore` trong luồng này. Database cũ được giữ trước khi tạo lại dự phòng bằng base backup từ active mới. Chiều Syncthing tự đổi theo vai trò như trước.

Mỗi mốc là một file `wal-<job-command>.manifest.json.gz` trong `/backups/postgres`, cùng biên nhận ở `/var/lib/immich-ha/runtime/wal-barriers`. Chúng chỉ mô tả file và WAL, không chứa bản sao database. Biên nhận ràng buộc job, command, source, epoch, system identifier và timeline. Retry sau khi PostgreSQL nguồn đã dừng đọc lại biên nhận, không khởi động lại nguồn để tạo dump. Danh mục này kiểm tra đường dẫn/kích thước từng file như trước; nó không thay thế một bản backup.

Bàn giao nhanh được bật cho job `switch` khi cả hai agent báo `fastHandoverVersion: 1`. Nguồn xác nhận app/ML inactive, PostgreSQL đã dừng, grant cục bộ đã xóa và mode `frozen` đã fsync cả file lẫn thư mục. Origin gate kiểm tra mode trên mỗi lần cấp quyền nên lease cũ không mở lại nguồn đã đóng băng. Worker chỉ bỏ khoảng đệm 30 giây khi biên nhận đúng job/epoch/nguồn, đích xác nhận đúng WAL/timeline và đủ file, kiểm tra cuối thành công, và nguồn có heartbeat sau biên nhận không quá 15 giây báo vẫn frozen/enforcing. Thiếu bất kỳ xác nhận nào thì vẫn dùng thời gian chờ cũ. Mất liên lạc hoặc promote thủ công ngoài job switch không được dùng lối nhanh. Vẫn cần thời gian promote, khởi động Immich và xác nhận cổng truy cập; không phải zero downtime. Audit ghi `fast-handover` nếu thực sự áp dụng.

Máy cũ trở lại sau sự cố được chặn app, dừng cụm PostgreSQL cũ nếu còn writable và tạo lại replica từ active mới. Bản này dùng **base backup**, chưa dùng `pg_rewind`; quá trình chỉ sao chép database, không sao chép lại toàn bộ ảnh. Các bản PGDATA cũ nằm trong `/mnt/immich-storage/backups/pg-stream/`, ngoài hai folder Syncthing. Chúng chưa có chính sách tự xóa; cần theo dõi dung lượng khi chuyển vai trò nhiều lần.

Không dùng replication slot cố định; `pg_basebackup` dùng slot tạm trong lúc sao chép. `wal_keep_size=512MB` và `max_wal_size=512MB` giúp hạn chế WAL tồn đọng, nhưng đây không phải giới hạn cứng dung lượng. Nếu peer offline lâu và WAL cần thiết đã bị loại bỏ, hệ thống cần tạo lại replica. Máy không nhận WAL quá 15 phút không được tự promote từ seed cũ.

## Theo dõi

PostgreSQL HA phải chờ IP Tailscale cục bộ trước khi khởi động. Bộ cài tạo drop-in `postgresql@16-ha.service.d/immich-tailnet.conf`, sắp thứ tự sau `tailscaled` và kiểm tra IP thực sự có trên `tailscale0` (tối đa 45 giây). Nếu IP chưa có, dừng lần khởi động thay vì chạy chỉ trên loopback. Runtime cũng kiểm tra điều kiện này khi start cụm HA; không tự restart database active. Nếu gặp lỗi cũ `pg_basebackup` với `Connection refused`, kiểm tra `ss -lnt 'sport = :5433'`: sau khi sửa thứ tự khởi động, cần restart có kế hoạch nếu PostgreSQL đang chạy mà thiếu listener Tailscale.

```bash
python tools/streaming-upgrade.py status
python admin.py status --compact
```

Dashboard hiển thị `PostgreSQL streaming`: active là nguồn dữ liệu, standby là đang nhận WAL / đang áp dụng WAL / mất kết nối. Đây là trạng thái nhận và áp dụng WAL cục bộ, không phải cam kết RPO bằng không. Số ảnh/video được đọc từ database cục bộ và làm mới khoảng một phút, nên màn hình vẫn có thể cập nhật lệch nhau trong khoảng đó.

Streaming hiện **bất đồng bộ**. Database và Syncthing là hai luồng riêng, nên sự cố đột ngột vẫn có thể xảy ra giữa lúc metadata và file chưa đến đủ. Bàn giao có kế hoạch kiểm tra WAL cùng danh mục file; không khẳng định không mất dữ liệu khi mất cả máy nguồn đột ngột. Giữ các bản backup độc lập và kiểm thử khôi phục định kỳ. Xem [tài liệu PostgreSQL về streaming replication](https://www.postgresql.org/docs/16/warm-standby.html).

Sau cutover, các lệnh SQL quản trị cần chọn `-p 5433 -d immich_stream`. Không trỏ app thủ công về `16/main` hoặc tự start các service Immich cũ.
