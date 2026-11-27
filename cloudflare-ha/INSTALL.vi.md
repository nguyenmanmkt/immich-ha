# Init/join: điều kiện và xử lý lỗi

Lệnh cài từ đầu nằm trong [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md): **cài Immich → gắn ổ → Cloudflare → init A → join B**. `init/join` chỉ cài HA, không cài Immich hoặc tạo ổ dữ liệu.

Bộ cài mới có tests, chưa nghiệm thu end-to-end trên LXC mới. Không chạy trên cụm HA hiện có.

## Điều kiện

- Debian 13 amd64; native Immich 3.4.1/3.4.2, PostgreSQL 16 và extension khớp trên hai máy.
- Immich đã hoàn tất migration, có bảng `public.asset`. A là nguồn; B chưa có ảnh hoặc thư viện riêng.
- Media root là mount thật `/mnt/immich-storage` trên cả hai máy, không thêm tầng `/library`.
- Rootfs còn ít nhất 3 GiB; B đủ chứa dữ liệu A và khoảng trống. Có backup độc lập trước migration.
- Cùng source/commit; digest file thực thi phải khớp.
- IP cố định/reservation. TCP 22000/5433 giữa A/B; HTTPS ra Worker/package repo và kết nối Cloudflare Tunnel.

## Mạng

`lan` dùng IPv4 RFC1918, không cần Tailscale. `tailscale` dùng IP tailnet, cần cài/login Tailscale trong hai LXC trước.

LAN cần tin cậy: PostgreSQL dùng SCRAM, chưa bật TLS; Tailscale mã hóa ở lớp VPN. Quorum tùy chọn dùng mTLS riêng. Thay IP sau cài cần cập nhật peer/bind/HBA, không chỉ nhãn hiển thị.

## Join và tiến trình

A hỏi cấu hình Cloudflare, tạo tài nguyên và cấp mã join 30 phút. B nhập Controller URL workers.dev, mã join và IP của chính B; không dùng URL dashboard có Access login.

Mã ràng buộc một thiết bị. Nếu hết hạn trước khi B ghép, tạo mã mới trên dashboard. Sau khi B ghép, không cấp mã để thay B bằng máy khác.

Client `immich-ha-bootstrap.service` tự chạy tác vụ cục bộ qua HTTPS. Luồng có dừng ghi để chốt database/file; giữ `16/main` và backup migration. Chỉ bật tự động khi active và replica đã sẵn sàng.

Sau cài, client chờ phần [witness tùy chọn](WITNESS-SETUP.vi.md); không tự cài witness. Agent HA hoạt động độc lập.

## Khi lỗi

Trong node có bước lỗi:

```bash
systemctl status immich-ha-bootstrap --no-pager
journalctl -u immich-ha-bootstrap -n 80 --no-pager
```

Receipt thành công được dùng lại khi mất phản hồi. Bước bị ngắt giữa chừng giữ dữ liệu và chặn chạy lại mù. **Thử lại bước lỗi** không bỏ các guard của database/cấu hình đã tồn tại.

Lỗi init giữa chừng: giữ `.install/init.json` và các tài nguyên đã tạo. Không xóa `.install/`, `generated/`, cluster PostgreSQL hoặc tự bật service cũ. Sau khi app HA đã nhận ghi, không tự quay về database cũ.

Sau cài cần xác nhận lịch SQL backup gốc và thử restore riêng. Cập nhật bản đang chạy: [MAINTENANCE.vi.md](MAINTENANCE.vi.md).
