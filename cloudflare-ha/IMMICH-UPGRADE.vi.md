# Nâng cấp Immich

Thực hiện trong tab **Nâng cấp**. Không chạy updater community-script trực tiếp trên HA vì nó điều khiển service/database mặc định, khác `immich-ha-app` và PostgreSQL `16/ha:5433`.

## Phiên bản hỗ trợ

Recipe hiện có: **3.4.1 → 3.4.2**, Debian 13 amd64, Node.js 24, PostgreSQL 16, VectorChord 1.1.1, CPU ML và layout `/opt/immich`. OpenVINO hoặc môi trường khác bị chặn nếu recipe không hỗ trợ.

Recipe ghim source/checksum và kiểm tra native dependencies. Thêm phiên bản mới cần đọc release notes, cập nhật recipe và kiểm chứng lại; không chỉ sửa số phiên bản.

## Thực hiện

1. Chờ hai node khỏe, không có job, WAL/file đã khớp; chọn phiên bản.
2. **Chuẩn bị:** build staging trên cả hai máy, giữ code đang chạy; tạm dừng tự chuyển máy.
3. Kiểm tra release notes, chọn **Nâng cấp cả hai** và nhập phiên bản để xác nhận.
4. Hệ thống dừng app active, chốt backup trước nâng cấp và xác nhận SQL.gz đã có/checksum đúng trên cả hai máy.
5. Cài code standby rồi active. Chỉ active có grant được chạy app mới/migration; replica không chạy Immich.
6. Xác nhận phiên bản, sức khỏe, WAL/file và khả năng tiếp quản trước khi khôi phục automation.

Có gián đoạn khi áp dụng. Owner được giữ nguyên; nếu đổi owner/epoch sau chuẩn bị, phải chuẩn bị lại. Đóng trình duyệt không hủy job.

Cần tối thiểu khoảng 2,3 GiB ổ hệ thống, 12 GiB media và RAM phù hợp để build. Staging/backup nâng cấp ở riêng từng máy, ngoài Syncthing; chỉ SQL backup phẳng được truyền theo rule cho phép. Backup trước nâng cấp không bật lại lịch dump HA cũ.

## Khi lỗi

| Giai đoạn | Xử lý |
| --- | --- |
| Chuẩn bị | Code/database đang chạy được giữ; xem `build.log`, sửa môi trường rồi chuẩn bị lại |
| Áp dụng | Giữ khóa bàn giao, automation tắt; sửa nguyên nhân rồi chọn **Tiếp tục nâng cấp** |
| Đã có thể nhận ghi bằng bản mới | Không tự hạ code hoặc restore database cũ vì sẽ mất thay đổi mới |

Giữ staging, checksum, backup và report để retry đúng job. Không dùng Resume chung, start service cũ hoặc chạy migration trên standby để bỏ qua bước lỗi.

Cập nhật tính năng/runtime của bản live là việc khác: [MAINTENANCE.vi.md](MAINTENANCE.vi.md).
