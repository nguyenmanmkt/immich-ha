# Thêm witness và Funnel sau init/join

**Tùy chọn:** hai node đã cài bằng `init/join` vẫn hoạt động mà không cần witness. Chỉ làm tài liệu này nếu muốn có điều phối và đường truy cập dự phòng khi Cloudflare gián đoạn.

Luồng mới chạy cục bộ qua HTTPS, **không SSH giữa các máy và không cần máy quản trị riêng**. Cloudflare vẫn điều phối chính. Logic fallback nằm trong agent A/B; witness giữ thành viên quorum thứ ba và gateway chung, không chứa PostgreSQL hoặc thư viện ảnh.

**Trạng thái kiểm chứng:** có tests cho logic HTTPS, khóa/quyền, LAN/Tailscale và payload cài cục bộ; chưa nghiệm thu end-to-end bộ cài này trên ba LXC mới. Bản live trước đây đã cài bằng tooling khác. Không chạy các lệnh init/join mới lên bản live hoặc cụm đã có quorum để chuyển đổi cách cài.

## 1. Điều kiện trước khi bắt đầu

- `init` A / `join` B đã hoàn thành trên cùng source/commit; dashboard báo active và dự phòng sẵn sàng, WAL/file đã đồng bộ, không có tác vụ/nâng cấp đang chạy.
- Giữ nguyên checkout `/opt/immich-ha` và dữ liệu riêng `.install/`, `.env`, `generated/` trên A. Không clone một bản mới rồi thay các định danh này.
- Tạo LXC witness trên **máy vật lý thứ ba**, độc lập A/B. Debian 13 amd64, gợi ý 1 vCPU, RAM 1 GiB, disk 8 GiB, bật onboot và đồng bộ giờ.
- Witness là LXC riêng, chưa cài Immich/etcd/gateway. Các cổng 2379, 2380 và 8090 phải còn trống trên cả ba máy.
- Có backup độc lập. Witness/quorum không phải backup ảnh hoặc database.

`primary` là ID node A đã chạy init, không phải luôn active. Lệnh tạo mã chạy trên A kể cả khi B đang active; cài witness giữ nguyên máy đang phục vụ và epoch.

## 2. Chọn mạng

Witness dùng **cùng chế độ mạng đã chọn khi init A/B**:

| Cách cài A/B | Địa chỉ witness nhập lúc join | Tailscale cần ở đâu? |
| --- | --- | --- |
| `lan` | IP LAN cố định/reservation, có route đến A và B | Không cần để cài witness/gateway |
| `tailscale` | IPv4 tailnet của chính LXC witness | Cài/login trong cả ba LXC |

Các địa chỉ phải là ba IP khác nhau, gắn trên đúng LXC. LAN cho phép RFC1918; Tailscale dùng `100.64.0.0/10`. Không nhập IP public, loopback hoặc IP của Proxmox host. Không tự đổi chế độ replication của A/B khi thêm witness.

Cho phép firewall giữa ba LXC:

| Kết nối | Mục đích |
| --- | --- |
| A/B/witness ↔ nhau, TCP 2379 và 2380 | Quorum mTLS, đa số 2/3 |
| Witness → A/B, TCP 8090 | Gateway đến node có grant |
| Thiết bị LAN/tailnet → witness, TCP 8090 | Truy cập gateway riêng nếu cần |
| Mỗi LXC → Worker và package/release repo, HTTPS | Join, nhận tác vụ cục bộ và tải package |

Không public cổng etcd, không tạo Tunnel/Funnel trỏ tới 2379/2380. SSH không phải điều kiện của luồng này. Quorum mã hóa bằng mTLS kể cả trên LAN.

### Chỉ khi chọn Tailscale hoặc muốn bật Funnel

Unprivileged LXC cần `/dev/net/tun`. Trên **Proxmox host đang chứa witness**, xem config LXC witness và bổ sung đúng các dòng còn thiếu khi LXC đã dừng:

```text
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Khởi động lại LXC witness, mở **console bên trong witness** rồi chạy:

```bash
ls -l /dev/net/tun
apt-get update
apt-get install -y curl ca-certificates
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
tailscale up --accept-dns=false --hostname=immich-ha-witness
tailscale ip -4
```

Mở URL đăng nhập do `tailscale up` in ra và phê duyệt máy trong cùng tailnet. Dùng root nên không cần `sudo`. Không chạy các lệnh cài này trên Proxmox host vì nhầm console.

Nếu A/B dùng LAN, bạn có thể bỏ toàn bộ nhánh này khi cài witness; đến lúc muốn Funnel chung mới cài Tailscale **chỉ trên witness**. Xem [Tailscale trong LXC](https://tailscale.com/docs/features/containers/lxc/lxc-unprivileged).

## 3. Chuẩn bị source trên witness

Trong **console witness**, lấy đúng commit như A/B; không lấy một bản mới hơn tùy tiện:

```bash
apt-get update
apt-get install -y git ca-certificates
git clone <URL_REPO_HTTPS> /opt/immich-ha
cd /opt/immich-ha
git checkout <COMMIT_DANG_DUNG_TREN_A_B>
```

Trên A có thể xem commit bằng `git -C /opt/immich-ha rev-parse HEAD`. Bộ cài còn kiểm tra digest các file thực thi, vì cùng commit nhưng source đã sửa cũng có thể khác nhau.

Không sao chép `.env`, API token hoặc `generated/` từ A sang witness. Witness sẽ nhận credential riêng sau khi join.

## 4. Tạo mã witness trên A

Trong **console LXC A**, dùng checkout đã init:

```bash
cd /opt/immich-ha
bash install.sh witness-init
```

Lệnh yêu cầu cụm hai node đang khỏe và chưa có quorum. Nó in **Controller URL** và **mã witness 30 phút**. Mã này khác mã ghép B, chỉ dành cho một witness.

Từ lúc yêu cầu thêm witness, tự chuyển máy/tự chuẩn bị dự phòng/tự trở về máy ưu tiên được tạm dừng để giữ nguyên vai trò trong lúc cài; dịch vụ Immich hiện tại tiếp tục chạy theo grant. Các lựa chọn trước đó chỉ được khôi phục sau khi cài và xác nhận xong. Vì vậy tạo mã khi đã sẵn sàng join, không để bước này chờ nhiều ngày.

Nếu mã hết hạn trước khi witness ghép, trên dashboard chọn **Tạo mã witness mới**. Mã cũ mất hiệu lực. Không khởi tạo lại Worker hoặc xóa state để lấy mã khác.

## 5. Join trên witness

Trong **console witness**:

```bash
cd /opt/immich-ha
bash install.sh witness-join
```

Nhập Controller URL `https://...workers.dev`, mã witness, chế độ `lan`/`tailscale` khớp A/B và IP của chính witness. Không dùng domain dashboard có trang Access login.

Sau khi join, `immich-ha-bootstrap.service` trên ba máy thực hiện các bước cục bộ theo thứ tự:

```text
Kiểm tra A/B/witness → A tạo CA và chứng thư riêng từng máy
→ cài/khởi động etcd ba thành viên → xác nhận quorum mTLS
→ bật khóa bắt buộc: witness, standby, active → kiểm tra grant
→ bật policy/fallback → cài gateway A/B → gateway chung witness
→ kiểm tra đăng nhập/định tuyến → khôi phục lựa chọn tự động
```

Không có script/shell tùy ý được gửi từ Worker. Client chỉ chạy danh mục tác vụ có trong source. CA private key giữ trên A tại `.install/witness-pki/`; mỗi thành viên chỉ nhận leaf key riêng. Dữ liệu PostgreSQL hiện có không bị stage lại hoặc tạo replica lại bởi bước này.

Dashboard hiển thị thẻ **Witness** và bước đang chạy. `Đã cấu hình` nghĩa là installer đã xác nhận các bước, không phải phép đo uptime liên tục của witness.

Nếu lỗi: tác vụ dừng, giữ cấu hình/checkpoint và giữ automation tắt. Không xóa marker `quorum-required`, PGDATA hoặc thư mục etcd để chạy lại. Xem mục xử lý lỗi bên dưới.

## 6. Thử gateway trước khi bật Funnel

Sau khi dashboard báo witness đã cấu hình, từ thiết bị cùng LAN hoặc tailnet mở:

```text
http://<IP_WITNESS_DA_JOIN>:8090
```

Đăng nhập bằng tài khoản Immich đang có, xem ảnh và upload một ảnh thử. Đây là gateway chung: route lấy từ quorum, không trỏ cố định A/B. Nếu không có grant hợp lệ hoặc không đọc được quorum thì trả 503.

Giữ domain Cloudflare làm đường chính. Gateway không thay đăng nhập Immich và không cung cấp trang quản trị HA khi Cloudflare mất.

Trong **console witness**, kiểm tra dịch vụ:

```bash
systemctl is-active immich-ha-quorum.service immich-ha-router.service nginx
journalctl -u immich-ha-bootstrap.service -n 60 --no-pager
```

**Điểm dừng:** chỉ qua bước tiếp theo khi đăng nhập, xem và upload qua gateway thành công.

## 7. Tùy chọn: một URL Funnel chung

1. Nếu witness chưa có Tailscale, hoàn tất nhánh Tailscale ở bước 2. Không cần cài Tailscale trên A/B chỉ để xuất bản URL chung nếu quorum/gateway đang dùng LAN.
2. Tailnet cần MagicDNS và HTTPS; tài khoản quản trị phải cho phép Funnel đối với witness. CLI có thể in URL phê duyệt `login.tailscale.com` nếu tính năng chưa được cấp. Xem [điều kiện Funnel](https://tailscale.com/docs/features/tailscale-funnel).
3. Trong **console witness**, chạy:

```bash
cd /opt/immich-ha
bash install.sh funnel-enable
```

Script chỉ cấu hình listener HTTPS 443 → `http://127.0.0.1:8090`, giữ nguyên Serve/Funnel không liên quan. Nếu cổng 443 đang dùng cho route khác, script dừng; không tự reset Serve để chiếm cổng. Nếu được in URL phê duyệt, mở nó, duyệt quyền rồi chạy lại `funnel-enable`.

Script kiểm tra DNS public, TLS, owner/epoch và đăng nhập trước khi in URL đã xác nhận. Nếu public DNS/certificate chưa sẵn sàng, nó có thể báo lỗi sau khi listener đã được bật; xem snapshot, đợi ổn rồi thử lại. Không kết luận Funnel dùng được chỉ từ thông báo CLI đã tạo route.

URL thường là:

```text
https://immich-ha-witness.<tailnet>.ts.net
```

URL lấy từ danh tính Tailscale thực tế; không sửa cứng hostname vào code và không trỏ Funnel thẳng cổng Immich 2283. Không cần thêm Tunnel/DNS/Access application trên Cloudflare cho URL `ts.net` này.

Sau khi xác nhận, dashboard tự hiện thẻ Funnel chung, URL/IP và công tắc. Witness nhận cài đặt qua client HTTPS, báo revision/trạng thái thật; khi mất liên lạc Cloudflare, giữ listener hiện tại. Công tắc chỉ điều khiển Funnel chung, không đổi vai trò A/B.

## 8. Thử truy cập Internet và chuyển active

1. Tắt Wi-Fi/Tailscale trên điện thoại, dùng 4G mở URL Funnel chung; đăng nhập, xem và upload.
2. Giữ nguyên URL, dùng dashboard thực hiện một lần chuyển active chủ động khi cả hai node đã sẵn sàng.
3. Khi hoàn thành, thử xem/upload lại. URL không đổi; vẫn có gián đoạn ngắn trong bàn giao, không cam kết zero downtime.
4. Xác nhận máy cũ trở thành replica và nhận WAL/file ngược.
5. Sau đó mới thử tình huống mất một node hoặc mất điều phối Cloudflare có kiểm soát; bài test Funnel không tự chứng minh chịu được các tình huống này.

Không dùng tool thử isolated hoặc script cài quorum cũ để gây sự cố trên cụm init/join mới. Runbook lỗi mạng/partition cần được chọn theo cụm đã cài; tuyệt đối không xóa state/lease để ép promote.

### Tùy chọn: URL Funnel riêng cho A/B

Không bắt buộc cho URL chung. Nếu muốn thêm đường vào trực tiếp khi witness gateway hỏng, cài/login Tailscale trong LXC tương ứng rồi chạy **trong từng LXC A/B**:

```bash
cd /opt/immich-ha
bash install.sh funnel-enable
```

Script trỏ vào gateway được gate của chính node đó. URL node đang standby trả 503; nó không tự route sang máy active. URL riêng được báo về dashboard trong cài đặt Funnel. Công tắc chung không tắt hai listener riêng.

## Xử lý lỗi và giữ dữ liệu riêng

Trên máy có bước lỗi:

```bash
systemctl status immich-ha-bootstrap.service --no-pager
journalctl -u immich-ha-bootstrap.service -n 80 --no-pager
```

Chi tiết lỗi CLI nằm trong `/opt/immich-ha/cloudflare-ha/.install/witness-local-error.log`; lỗi tác vụ cục bộ trong `.install/receipts/*.error`. Đây là file riêng root-only, không đăng nguyên nội dung lên issue. Receipt thành công được phát lại khi mất phản hồi; tác vụ bị ngắt giữa chừng bị chặn chạy lại mù. Chưa có nút tự sửa/reset mọi kiểu cài dở; cần kiểm tra checkpoint trước khi phục hồi.

- Join không nhận: kiểm tra mã witness, đúng commit/digest, Controller URL và cùng network mode.
- Không có quorum: kiểm tra IP gắn trên từng LXC, firewall 2379/2380 và log `immich-ha-quorum.service`; không tắt mTLS.
- Gateway 503: kiểm tra grant/quorum và tiến trình chuyển máy; không start Immich standby bằng tay.
- Funnel lỗi: kiểm tra Tailscale Running, HTTPS/Funnel policy và listener 443; snapshot ở `/etc/immich-ha/funnel-backups/`.
- Mất witness: nếu A/B còn liên lạc thì có thể vẫn đủ đa số 2/3; gateway/Funnel chung trên witness mất cùng nó. Mất đa số thì không cấp quyền mới.

Giữ riêng `.install/`, `.env`, `generated/`, chứng thư/khóa và data etcd. Không đồng bộ PGDATA hoặc data etcd qua Syncthing. Các công cụ SSH trong [QUORUM.vi.md](QUORUM.vi.md) là runbook cũ, không phải các bước nối tiếp của hướng dẫn này.
