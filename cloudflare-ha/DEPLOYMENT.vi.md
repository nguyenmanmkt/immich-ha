# Triển khai Immich HA

Thứ tự: **Immich A/B → volume ảnh → Cloudflare → init A → join B → nghiệm thu**. Witness/Funnel là tùy chọn sau đó.

**Bộ cài mới có tests, chưa nghiệm thu end-to-end trên LXC mới.** Không chạy lại trên HA đang hoạt động. Yêu cầu: Debian 13 amd64, Immich 3.4.1/3.4.2 và PostgreSQL 16 khớp; hai node thấy nhau qua LAN/Tailscale. [Điều kiện chi tiết](INSTALL.vi.md).

## 1. Cài Immich và ổ dữ liệu

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

Đúng đường dẫn là **`/mnt/immich-storage`**, không thêm `/library`. Xong cả hai máy, làm [Cloudflare setup](CLOUDFLARE-SETUP.vi.md), rồi tiếp tục bước 2.

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

## 3. Nghiệm thu

- Dashboard: một active, một replica sẵn sàng; WAL và Syncthing đồng bộ.
- Đăng nhập, xem/upload qua domain Immich; thử chuyển active và đồng bộ ngược.
- Xác nhận lịch SQL backup gốc trên Immich, bản sao trên peer và thử restore riêng.

## 4. Tùy chọn witness/Funnel

Làm theo [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md): `witness-init` trên A → `witness-join` trên witness → thử gateway → tùy chọn `funnel-enable`.

Hai node cơ bản phụ thuộc Cloudflare và đóng gate khi grant hết hạn. Witness/fallback bổ sung điều phối khi mất Cloudflare; Funnel bổ sung URL truy cập riêng. Không tự cài các thành phần này mặc định.

## Hệ thống đã chạy

Không dùng init/join để cập nhật. Giữ nguyên cấu hình, khóa và database; xem [MAINTENANCE.vi.md](MAINTENANCE.vi.md). Vận hành: [DASHBOARD.vi.md](DASHBOARD.vi.md).
