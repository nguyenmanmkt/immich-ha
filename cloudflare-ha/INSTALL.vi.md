# Init trên A, join trên B — không SSH

Mỗi LXC tự cài cục bộ và chủ động kết nối HTTPS ra Worker. Không cần laptop/máy quản trị riêng, SSH key hay mật khẩu root của node còn lại.

**Bộ cài mới chưa được nghiệm thu end-to-end trên hai LXC cài mới.** Unit/controller tests không thay một lần cài thật. Không chạy init/join trên HA đang hoạt động; installer từ chối config/cluster đã có.

**Chưa cài Immich hoặc gắn ổ?** Làm [bước 1 trong DEPLOYMENT.vi.md](DEPLOYMENT.vi.md#1-chuẩn-bị) trước: tạo hai LXC → gắn volume → đặt media root. `init/join` chỉ cài HA, không cài Immich hoặc tạo ổ dữ liệu.

Bộ cài này chỉ dựng hai node do Cloudflare điều phối. Nó không cài quorum, witness hoặc Funnel; khi mất Cloudflare, không có điều phối thay thế và gate đóng khi grant hết hạn. Phần mở rộng được tách tại [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md).

## Điều kiện

- Hai Debian amd64 LXC đã cài native Immich **3.4.1 hoặc 3.4.2**, cùng bản và extension PostgreSQL 16.
- Immich native đã khởi động và hoàn tất migration trên cả hai máy (có bảng `public.asset`). B chưa có ảnh; database trống hoàn toàn chưa khởi tạo schema chưa đủ điều kiện join.
- A chứa dữ liệu nguồn, app đã dùng `/mnt/immich-storage`. B cài mới, chưa có ảnh/video/thư viện riêng.
- Hai media root là mount thật. B đủ chứa dữ liệu A + khoảng trống; mỗi rootfs còn ít nhất 3 GiB.
- Cùng source/commit trên hai máy; khi join có kiểm tra digest bundle.
- Cloudflare zone active, Account ID, Zone ID, API token; workers.dev đã kích hoạt. Dùng tên Worker/domain mới chưa có DNS/app trùng. Hoàn tất bước kích hoạt/điều khoản Zero Trust nếu tài khoản yêu cầu.
- API token có quyền Worker/script/subdomain/Cron/Custom Domain, Tunnel, DNS, Access apps/policies, organization/identity provider và service token, trong đúng account/zone.

**Chọn mạng:** `lan` dùng IP RFC1918 trực tiếp, **không cài hoặc yêu cầu Tailscale**. `tailscale` dùng IP tailnet, cần cài/login Tailscale trước. Hai node phải thông TCP 22000 và 5433; ra HTTPS tới Worker/package repo và ra Cloudflare Tunnel (7844 theo cấu hình mạng).

Dùng IP cố định hoặc DHCP reservation cho peer; đổi địa chỉ sau cài cần cập nhật bind/HBA và cấu hình replication có kế hoạch, không chỉ sửa tên hiển thị.

LAN cần là mạng tin cậy: replication hiện dùng SCRAM, kết nối PostgreSQL không bật TLS; Tailscale mã hóa ở lớp VPN. PostgreSQL chỉ bind loopback/IP đã chọn, replication chỉ cho peer `/32`. Syncthing có kết nối mã hóa riêng.

## 1. Trên LXC A

Chưa biết lấy ID/token hoặc chuẩn bị trang quản lý? Hoàn tất [Cloudflare setup](CLOUDFLARE-SETUP.vi.md) trước; script tự tạo Worker, dashboard, Access và hai Tunnel.

Mở console/root **LXC Immich A**. Nếu thiếu Git, cài package `git` trước. Lấy source qua HTTPS, không clone đè `/opt/immich`:

```bash
git clone https://github.com/nguyenmanmkt/immich-ha.git /opt/immich-ha
cd /opt/immich-ha
bash install.sh init
```

Wizard hỏi chế độ mạng/IP của A, Cloudflare Account ID/Zone ID/API token (nhập ẩn), tên Worker, domain Immich/dashboard, email đăng nhập và LXC IDs để hiển thị.

Script tạo Access email OTP cho dashboard, service token/policy origin, hai Tunnel/DNS origin, Worker/Durable Object/Cron và Custom Domains. Domain app dùng đăng nhập Immich, không áp trang Access tương tác; origin vẫn có service auth/gate. Không ghi đè tài nguyên tên/domain đã tồn tại.

Terminal trả **Controller URL**, **mã join 30 phút** và URL dashboard. Mã chỉ ghép B, không commit hoặc gửi vào issue.

Nếu mã hết hạn trước khi B ghép, đăng nhập dashboard và chọn **Tạo mã join mới** ở thẻ thiết lập. Mã cũ vô hiệu, mã mới vẫn chỉ dành cho B. Sau khi B đã ghép, không cấp mã cho thiết bị khác.

## 2. Trên LXC B

Dùng đúng source/commit như A:

```bash
git clone https://github.com/nguyenmanmkt/immich-ha.git /opt/immich-ha
cd /opt/immich-ha
bash install.sh join
```

Nhập Controller URL `https://...workers.dev` do A cấp, mã join, cùng chế độ mạng và IP của B. Không dùng URL dashboard có Access login để join. Không nhập SSH key/mật khẩu của A.

Nếu B có dữ liệu, thiếu mount, sai IP/version/extension, cài đặt dừng trước khi cài lên hai node. Mã đã ghép không dùng cho thiết bị khác; cùng thiết bị có claim cục bộ để nhận lại phản hồi bị mất trong thời hạn mã.

## 3. Theo dõi trên dashboard

Mở domain dashboard, đăng nhập email đã chọn. Thẻ **Thiết lập cụm** hiển thị:

```text
Kiểm tra A/B → agent/Tunnel → ghép Syncthing → đồng bộ ảnh
→ runtime/gates → stage database HA → tạo replica
→ dừng ghi/copy cuối → xác nhận WAL/file → bật streaming
→ xác nhận hai node → bật tự động
```

A luôn là nguồn khi bootstrap. Có gián đoạn khi dừng app A để chốt database cuối. Cluster `16/main` và backup migration được giữ; không tự xóa. B vào streaming chỉ làm replica, Immich B dừng. Khi xác nhận cả hai sẵn sàng, bật tự chuyển sự cố và tự chuẩn bị dự phòng. Tự trở về máy ưu tiên/Slack cấu hình riêng trên UI.

Khởi động agent mới có thể phải chờ session/grant cũ hết hạn trước khi app được mở lại. Không giảm thời gian fencing hoặc tự start app để làm nhanh bước này.

Quy tắc truyền SQL backup gốc được bật; lịch backup native và bài restore thử vẫn cần xác nhận trong Immich. Witness/quorum fallback/Funnel là [mở rộng riêng sau đó](WITNESS-SETUP.vi.md); không bắt buộc cho init/join hai node. Có `witness-init` trên A, `witness-join` trên witness và `funnel-enable` tùy chọn, đều chạy cục bộ không SSH.

## Khi bị ngắt hoặc lỗi

Trên node tương ứng:

```bash
systemctl status immich-ha-bootstrap --no-pager
journalctl -u immich-ha-bootstrap -n 80 --no-pager
```

Client tự nối lại HTTPS và gửi lại receipt. Tác vụ có receipt thành công không chạy lại. Nếu bị ngắt giữa tác vụ chưa ghi xong receipt, nó báo lỗi và giữ file, không tự ghi đè cluster. Nút **Thử lại bước lỗi** vẫn chịu các guard; không dùng để bỏ kiểm tra hoặc reset cluster tạo dở.

Nếu lỗi sau dừng ghi, giữ dữ liệu và xem log; không tự bật service cũ hoặc xóa `generated/`/`.install/`. Lỗi API giữa init có thể để lại tài nguyên đã tạo, được ghi tại `.install/init.json`; cần xử lý checkpoint đó trước khi khởi tạo lại. Không có rollback mù về database cũ sau khi app mới đã nhận ghi.

Cài đặt chạy trong `immich-ha-bootstrap.service`, không cần giữ terminal. Xong thì client chờ mở rộng witness tùy chọn qua HTTPS; agent HA chạy độc lập. Client rảnh trên A/B kiểm tra khoảng mỗi phút, không tự cài witness. Giữ `.env`, `project.json`, `generated/`, `.install/` riêng tư. A giữ API token triển khai; B chỉ nhận credential của chính nó.

Luồng thủ công/bảo trì hệ thống cũ nằm trong [MANUAL-DEPLOYMENT.vi.md](MANUAL-DEPLOYMENT.vi.md); các tool SSH ở đó không phải dependency của init/join.

API tham chiếu: [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/), [Worker Domains](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/update/), [Access OTP](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/), [service tokens](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/service_tokens/methods/create/).
