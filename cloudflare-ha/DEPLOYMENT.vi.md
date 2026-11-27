# Triển khai Immich HA

Chạy `init` trong LXC A, `join` trong LXC B; sau đó theo dõi trên dashboard. Hai node tự cài cục bộ qua HTTPS, không cần máy quản trị riêng hoặc SSH sang nhau. LAN dùng trực tiếp; Tailscale là lựa chọn khi cần kết nối hai mạng.

**Bộ cài init/join chưa được nghiệm thu trên hai LXC cài mới.** Đã có kiểm thử tự động cho logic cài đặt, nhưng chưa đủ để gọi là cài thực tế thành công. Không chạy lại trên hệ thống HA đang hoạt động.

## 1. Chuẩn bị

Thứ tự: **hai LXC Immich và ổ dữ liệu → Cloudflare/domain/API token → init A → join B → đăng nhập dashboard → nghiệm thu**. Witness/Funnel chỉ thêm sau khi hai node cơ bản đã ổn.

- Hai LXC Debian amd64 đã có native Immich cùng phiên bản được hỗ trợ: 3.4.1 hoặc 3.4.2, PostgreSQL 16 và extension tương thích.
- A chứa thư viện nguồn tại mount `/mnt/immich-storage`; B chưa có dữ liệu riêng, có mount cùng đường dẫn và đủ dung lượng.
- IP hai LXC ổn định, thấy nhau qua LAN hoặc Tailscale đã đăng nhập. Thông TCP 22000 và 5433 giữa hai node; có Internet để cài package, kết nối Worker và Tunnel.
- Cloudflare Account ID, Zone ID, API token đủ quyền; zone active và workers.dev đã bật. Chuẩn bị domain Immich, dashboard và email đăng nhập. Dùng tên/domain chưa được tài nguyên khác sử dụng.
- Có backup độc lập trước khi chuyển database. Streaming/Syncthing không thay backup.

Làm theo [chuẩn bị Cloudflare từng bước](CLOUDFLARE-SETUP.vi.md): bật workers.dev/Zero Trust, lấy Account ID/Zone ID và tạo API token đúng quyền. **Không cần tự tạo Worker hoặc trang quản lý trước**; script tạo chúng khi init. Dung lượng và hạn chế mạng nằm trong [hướng dẫn init/join](INSTALL.vi.md#điều-kiện).

## 2. Cài hai node

Lấy cùng source/commit tại `/opt/immich-ha` trên hai LXC. Thay `<URL_REPO_HTTPS>` bằng repo của bạn.

**Console LXC A:**

```bash
git clone <URL_REPO_HTTPS> /opt/immich-ha
cd /opt/immich-ha
bash install.sh init
```

Nhập mạng/IP, thông tin Cloudflare, domain và email theo wizard. Giữ Controller URL và mã join được in ra.

**Console LXC B:**

```bash
git clone <URL_REPO_HTTPS> /opt/immich-ha
cd /opt/immich-ha
bash install.sh join
```

Nhập Controller URL, mã join và IP B. Mở dashboard bằng email đã chọn để theo dõi. Cài đặt có thời điểm dừng Immich A để chốt dữ liệu; không tự start dịch vụ trong lúc này.

Script tạo Worker/Access/Tunnel, cài agent/gate/Syncthing, đồng bộ file và chuyển sang PostgreSQL streaming. B là replica, Immich B dừng khi ở vai trò dự phòng. [INSTALL.vi.md](INSTALL.vi.md) mô tả từng bước, đổi mã join và xử lý gián đoạn.

## 3. Xác nhận trước khi sử dụng

- Dashboard: một active, một dự phòng sẵn sàng; PostgreSQL nhận WAL, Syncthing đồng bộ.
- Đăng nhập, xem và upload qua domain Immich.
- Kiểm tra lịch backup SQL gốc trong Immich và bản sao trên node còn lại; thực hiện restore thử riêng.
- Thử bàn giao chủ động, sau đó thử sự cố và quay lại; kiểm tra ảnh mới qua mỗi lần chuyển.

Đây là các bài nghiệm thu cần làm trên hệ thống thực tế. Unit/controller tests không thay các bước này.

## 4. Tùy chọn: dự phòng khi mất Cloudflare

Hai node cơ bản vẫn phụ thuộc Cloudflare để nhận quyền phục vụ. Khi không nhận được grant mới, gate đóng sau khi grant hết hạn; bộ cài không tự tạo đường truy cập hoặc bộ điều phối thay thế.

Nếu cần tiếp tục hoạt động khi Cloudflare gián đoạn, làm theo [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md), từng bước không SSH:

1. Witness độc lập và quorum ba thành viên A/B/witness.
2. Khóa chung bắt buộc, rồi bật fallback sau khi kiểm chứng fencing.
3. Gateway LAN/Tailscale, tiếp đến Funnel chung nếu cần URL HTTPS dự phòng.

Trên A chạy `bash install.sh witness-init`, trên witness chạy `bash install.sh witness-join`. Sau khi thử gateway thành công, tùy chọn chạy `bash install.sh funnel-enable` trên witness. Mạng quorum theo LAN/Tailscale đã chọn cho A/B; Funnel cần Tailscale riêng trên máy xuất bản URL.

Cloudflare vẫn điều phối chính. Khi nó mất kết nối, logic fallback trong agent A/B dùng cùng khóa quorum; witness cung cấp thành viên quorum và gateway, không phải một Immich thứ ba. Bộ cài mở rộng mới có tests, **chưa nghiệm thu trên ba LXC mới**. Bản live đã triển khai lớp này bằng tooling cũ; không chạy lại để đổi cách cài.

## Hệ thống đã chạy

Không dùng `init/join` để cập nhật hoặc sửa lỗi bản live. Giữ nguyên `.env`, `project.json`, `generated/`, khóa, định danh Durable Object và database hiện có. Dùng [runbook bảo trì](MANUAL-DEPLOYMENT.vi.md#12-cập-nhật-hệ-thống-đã-chạy); các tool SSH trong runbook là công cụ triển khai/bảo trì, không phải giao tiếp runtime giữa hai node.

Vận hành: [DASHBOARD.vi.md](DASHBOARD.vi.md). Cơ chế hoạt động: [ARCHITECTURE.vi.md](ARCHITECTURE.vi.md).
