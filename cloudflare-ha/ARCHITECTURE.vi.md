# Cơ chế hoạt động

## Ba lớp

| Lớp | Thành phần | Trách nhiệm |
| --- | --- | --- |
| Dữ liệu | PostgreSQL streaming, Syncthing | Đồng bộ database và media |
| Điều phối | Worker/Durable Object, agent; quorum tùy chọn | Cấp quyền active, lease, epoch và thứ tự bàn giao |
| Truy cập | Worker/Tunnel/gate; gateway/Funnel tùy chọn | Đưa request tới node có quyền |

Cloudflare điều phối chính. Witness là phần mở rộng, không phải database thứ ba. `primary`/`backup` là ID node; `owner` là máy đang active, `epoch` tăng khi đổi quyền.

## Vận hành bình thường

- Active chạy Immich đầy đủ worker và PostgreSQL writable.
- Standby chạy PostgreSQL recovery; Immich dừng.
- PostgreSQL truyền WAL; Syncthing truyền media active **Send Only** → standby **Receive Only**.
- Agent nhận grant qua HTTPS. Gate chỉ phục vụ khi grant/epoch/mode hợp lệ; app 2283 chỉ nghe loopback.
- Nếu bật witness, mọi quyền ghi còn phải được khóa quorum mTLS chấp thuận.

## Bàn giao chủ động

```text
Chuẩn bị replica → chốt WAL/file → đồng bộ và kiểm tra
→ dừng ghi, chốt cuối → fencing nguồn → cấp quyền mới
→ promote và mở Immich → đưa máy cũ về dự phòng
```

Luồng này không tạo/nạp dump. Bàn giao nhanh chỉ bỏ khoảng đệm khi có bằng chứng fencing bền vững và đích đủ WAL/file; vẫn cần thời gian promote/khởi động app. Không cam kết zero downtime.

## Sự cố và hồi phục

Coordinator xác nhận lỗi, chờ quyền cũ hết hiệu lực và kiểm tra replica trước khi promote. Mặc định: lease Cloudflare 240 giây, fencing margin 30 giây, xác nhận lỗi 300 giây; các mốc có thể chồng lấn.

Máy cũ trở lại không tự mở ghi. Nó nhận dữ liệu từ owner mới và được kiểm tra trước khi làm dự phòng. Nếu bật trở về máy ưu tiên, sau khoảng 2 phút ổn định hệ thống mới bắt đầu một lần bàn giao đầy đủ.

## Khi mất Cloudflare

- **Hai node cơ bản:** không có coordinator thay thế; gate đóng khi grant hết hạn.
- **Có witness/fallback:** agent A/B dùng cùng khóa quorum để giữ writer hoặc tiếp quản replica đủ điều kiện. Cloudflare trở lại phải đối chiếu với writer thực tế bằng nonce/CAS.
- Quorum cần đa số 2/3. Mất đa số không cấp quyền mới; không dùng ping để đoán owner.

Chi tiết: [QUORUM.vi.md](QUORUM.vi.md). Cài tùy chọn: [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md).

## Đường truy cập

| Đường | Hành vi |
| --- | --- |
| Domain chính | Worker chọn node có grant qua Tunnel |
| Gateway/Funnel riêng | Chỉ phục vụ ở node active; standby trả 503 |
| Gateway/Funnel witness | Một URL, đọc owner quorum rồi route tới active |

Gateway witness chỉ đọc, không promote; không xác nhận được route thì đóng. Proxy không tự replay POST/upload sang node khác. Mất witness làm mất đường chung; các đường còn lại vẫn phụ thuộc grant của chúng.

## Dữ liệu cần giữ

Backup độc lập gồm media, SQL database tương ứng và cấu hình/khóa deployment. Streaming/Syncthing không thay backup lịch sử: file xóa có thể được truyền xóa theo. PGDATA archive, manifest WAL, dump migration và SQL backup là các loại khác nhau; không đồng bộ PGDATA qua Syncthing.
