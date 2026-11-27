# Witness và Funnel — tùy chọn

Chỉ thêm sau khi init/join A/B hoàn tất, hai node khỏe và không có job. Cloudflare vẫn điều phối chính; witness bổ sung quorum/gateway, không chứa database hoặc media.

**Bộ cài mới có tests, chưa nghiệm thu end-to-end trên ba LXC mới.** Không dùng để cài lại witness/quorum của bản live.

## 1. Chuẩn bị witness

- LXC Debian 13 amd64 trên **host vật lý thứ ba**; gợi ý 1 CPU, RAM 1 GiB, disk 8 GiB, bật onboot.
- Chưa có Immich, etcd hoặc gateway; cổng 2379/2380/8090 trống trên cả ba LXC.
- Cùng source/commit với A/B; giữ nguyên checkout và cấu hình riêng trên A/B.

| Mạng A/B khi init | IP witness nhập lúc join |
| --- | --- |
| `lan` | IP LAN cố định/reservation, route được tới A/B |
| `tailscale` | IPv4 tailnet riêng của LXC witness, cùng tailnet A/B |

Mở TCP 2379/2380 giữa ba node, TCP 8090 từ witness tới A/B và HTTPS ra Worker/package repo. Không public etcd. Quorum luôn dùng mTLS; không cần SSH.

### Tailscale: chỉ khi chọn tailnet hoặc muốn Funnel

Với unprivileged LXC, trên **Proxmox host chứa witness**, dừng LXC rồi thêm các dòng còn thiếu vào config; không thêm trùng:

```text
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Khởi động LXC, mở **console witness**:

```bash
apt-get update
apt-get install -y curl ca-certificates
ls -l /dev/net/tun
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
tailscale up --accept-dns=false --hostname=immich-ha-witness
tailscale ip -4
```

Mở link đăng nhập được in ra để duyệt máy. Nếu A/B dùng LAN, có thể bỏ nhánh này đến lúc bật Funnel; Funnel chung chỉ cần Tailscale trên witness. Tham khảo [Tailscale LXC](https://tailscale.com/docs/features/containers/lxc/lxc-unprivileged).

## 2. Lấy source trên witness

Trên A xem commit bằng `git -C /opt/immich-ha rev-parse HEAD`. Trong **console witness**, dán commit đó khi được hỏi:

```bash
apt-get update && apt-get install -y git ca-certificates
git clone https://github.com/nguyenmanmkt/immich-ha.git /opt/immich-ha
cd /opt/immich-ha
read -rp 'Commit tren A/B (40 ky tu): ' IMMICH_HA_COMMIT
[[ "$IMMICH_HA_COMMIT" =~ ^[a-f0-9]{40}$ ]] && git checkout --detach "$IMMICH_HA_COMMIT"
```

Không copy `.env`, `generated/` hoặc API token từ A sang witness.

## 3. Tạo mã và join

**Console A** — node đã chạy init, dù hiện là active hay standby:

```bash
cd /opt/immich-ha
bash install.sh witness-init
```

Giữ Controller URL và mã witness 30 phút. Automation tạm dừng từ bước này đến khi cài xong; chỉ tạo mã khi đã sẵn sàng. Mã hết hạn trước khi join: tạo mã mới trên dashboard.

**Console witness:**

```bash
cd /opt/immich-ha
bash install.sh witness-join
```

Nhập Controller URL **workers.dev**, mã witness, cùng network mode và IP witness. Mỗi máy tự cài qua HTTPS: kiểm tra → chứng thư → etcd → grant bắt buộc → fallback → gateway → xác nhận. CA private key giữ trên A; witness chỉ nhận credential riêng.

Dashboard hiển thị tiến độ; automation chỉ khôi phục sau kiểm tra. `Đã cấu hình` là kết quả cài đặt, không phải phép đo uptime liên tục.

## 4. Thử gateway

Mở `http://<IP_WITNESS>:8090` từ LAN/tailnet, đăng nhập Immich và thử xem/upload. Chỉ bật Funnel sau khi bước này thành công.

Trong **console witness**:

```bash
systemctl is-active immich-ha-quorum immich-ha-router nginx
```

Gateway theo owner trong quorum; không có grant hoặc không xác nhận được route thì trả 503.

## 5. Bật Funnel chung nếu cần

Witness phải có Tailscale Running, MagicDNS/HTTPS và quyền Funnel. Xem [điều kiện Funnel](https://tailscale.com/docs/features/tailscale-funnel).

Trong **console witness**:

```bash
cd /opt/immich-ha
bash install.sh funnel-enable
```

Nếu CLI in link phê duyệt Tailscale, mở link, duyệt rồi chạy lại. Script dùng HTTPS 443 → gateway 8090, không chiếm route khác hoặc reset Serve. URL được lấy từ Tailscale, ví dụ `https://immich-ha-witness.<tailnet>.ts.net`; không thêm Tunnel Cloudflare.

Script xác nhận public DNS/TLS, route và đăng nhập trước khi báo thành công. Dashboard tự nhận URL/IP và công tắc; witness giữ listener hiện tại khi Cloudflare mất liên lạc.

Muốn URL riêng A/B: cài Tailscale trên node tương ứng rồi chạy cùng lệnh `funnel-enable` tại node đó. URL riêng không tự đổi máy; standby trả 503. Công tắc chung chỉ điều khiển witness.

## 6. Nghiệm thu

1. Dùng 4G, tắt Tailscale trên điện thoại; đăng nhập/xem/upload qua Funnel chung.
2. Chuyển active trên dashboard, giữ nguyên URL và thử lại.
3. Xác nhận node cũ nhận WAL/file ngược và trở thành dự phòng.

Test mất node, mất Cloudflare và mất đa số là các bài riêng. Không dùng script thử isolated cũ trên cụm đã enforced.

## Khi lỗi

```bash
systemctl status immich-ha-bootstrap --no-pager
journalctl -u immich-ha-bootstrap -n 80 --no-pager
```

Chi tiết root-only trong `cloudflare-ha/.install/witness-local-error.log` hoặc `.install/receipts/*.error`; không đăng nguyên file lên issue. Receipt thành công không chạy lại; bước bị ngắt giữ checkpoint và chặn retry mù.

Giữ automation tắt khi rollout lỗi. Không xóa `quorum-required`, data etcd, PGDATA hoặc CA để cài lại. Mất witness làm mất gateway/Funnel chung; A/B vẫn có thể giữ đa số 2/3 nếu còn liên lạc.
