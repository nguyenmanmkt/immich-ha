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

### Cài Immich trên cả hai LXC mới

Trên **mỗi Proxmox host**, chạy script dưới để tạo một LXC Immich. Chọn Debian 13 **amd64**, gợi ý 4 CPU / 6 GiB RAM / rootfs 20 GiB. Làm hai lần để có A và B; chưa upload ảnh.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/immich.sh)"
```

[Script upstream](https://github.com/community-scripts/ProxmoxVE/blob/main/ct/immich.sh) có thể đổi phiên bản; bộ HA hiện nhận Immich 3.4.1/3.4.2 và PostgreSQL 16.

### Gắn volume ảnh trên từng Proxmox host

Mẫu này tạo **volume mới trên storage Proxmox có sẵn**, không format HDD vật lý. Chạy cho A và B, nhập đúng ID LXC/storage của host đó. Dung lượng B phải đủ chứa ảnh A.

```bash
(
set -euo pipefail
read -rp 'ID LXC Immich: ' CTID
read -rp 'Storage ID (vd local-lvm): ' STORAGE
read -rp 'Dung luong volume moi (GiB): ' SIZE
[[ "$CTID" =~ ^[0-9]+$ && "$SIZE" =~ ^[1-9][0-9]*$ ]]
if pct config "$CTID" | grep -Eq '^mp0:|mp=/mnt/immich-storage(,|$)'; then
  echo 'Da co mount: giu nguyen, khong tao de.'; exit 1
fi
pct shutdown "$CTID" --timeout 60
pct set "$CTID" -mp0 "$STORAGE:$SIZE,mp=/mnt/immich-storage,backup=1"
pct start "$CTID"
pct exec "$CTID" -- df -hT /mnt/immich-storage
)
```

### Chọn thư mục lưu ảnh trong mỗi LXC

Mở **Console của LXC A rồi B**, chạy cùng khối dưới. Chỉ dùng cho bản cài mới chưa có ảnh; máy đã có dữ liệu/mount thì giữ nguyên và bỏ bước chuyển thư mục này.

```bash
(
set -euo pipefail
mountpoint -q /mnt/immich-storage
[[ "$(runuser -u postgres -- psql -XAt -p 5432 -d immich -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM asset;')" == 0 ]] || {
  echo 'Database da co anh: dung, khong doi duong dan bang mau cai moi.'; exit 1;
}
test -d /opt/immich/upload
grep -q '^IMMICH_MEDIA_LOCATION=' /opt/immich/.env
systemctl stop immich-web.service immich-ml.service
cp -an /opt/immich/.env /opt/immich/.env.before-storage
cp -a /opt/immich/upload/. /mnt/immich-storage/
chown immich:immich /mnt/immich-storage
sed -i 's|^IMMICH_MEDIA_LOCATION=.*|IMMICH_MEDIA_LOCATION=/mnt/immich-storage|' /opt/immich/.env
systemctl start immich-ml.service immich-web.service
grep '^IMMICH_MEDIA_LOCATION=' /opt/immich/.env
df -hT /mnt/immich-storage
psql --version
python3 -c 'import json; print(json.load(open("/opt/immich/app/package.json"))["version"])'
)
```

Đúng đường dẫn là **`/mnt/immich-storage`**, không thêm `/library`. Xong cả hai máy, chuẩn bị Cloudflare ở link trên rồi làm bước 2.

## 2. Cài hai node

Lấy cùng source/commit từ repo dưới đây tại `/opt/immich-ha` trên hai LXC.

**Console LXC A:**

```bash
apt-get update && apt-get install -y git
git clone https://github.com/nguyenmanmkt/immich-ha.git /opt/immich-ha
cd /opt/immich-ha
bash install.sh init
```

Nhập mạng/IP, thông tin Cloudflare, domain và email theo wizard. Giữ Controller URL và mã join được in ra.

**Console LXC B:**

```bash
apt-get update && apt-get install -y git
git clone https://github.com/nguyenmanmkt/immich-ha.git /opt/immich-ha
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
