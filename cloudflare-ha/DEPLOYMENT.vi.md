# Triển khai Immich HA

Luồng chuẩn: hai Immich native LXC → Tailscale/Syncthing → Worker/gate → agent/runtime v2 → streaming → kiểm tra → bật tự động. Các lệnh ở dưới là thao tác thật; làm xong điểm kiểm tra của mỗi bước rồi mới tiếp tục.

Bản đầy đủ có ba phần: HA dữ liệu trên hai LXC, quorum/fallback trên hai node và witness, rồi gateway/Funnel dự phòng. Đọc [cách hoạt động](ARCHITECTURE.vi.md) trước khi chọn điểm bắt đầu. Mục 1–10 dựng HA dữ liệu; mục 11 bổ sung quorum và đường truy cập dự phòng. Hệ thống cũ đang chạy chỉ dùng mục cập nhật.

## 0. Chọn đúng nơi chạy lệnh

| Nơi chạy | Công việc |
| --- | --- |
| **Proxmox host** | Tạo LXC, gắn ổ/mountpoint, cấp TUN, bật onboot |
| **Bên trong từng Immich LXC** | Immich, Tailscale, Syncthing, Nginx, agent, PostgreSQL |
| **Máy quản trị có source/venv** | `.env`, `project.json`, deploy Worker, đóng gói và SSH rollout |
| **Cloudflare dashboard** | Zone, Access, Tunnel Published Application và Worker Custom Domain |

Trước khi cài package, chạy `hostname` để biết đang ở host hay LXC. Ví dụ trên **Proxmox host**: `pct enter 113` để vào LXC A; ID phải được thay bằng ID thực tế của bạn. Tailscale dùng cho replication phải nằm trong LXC Immich, không chỉ nằm trên Proxmox host.

Nếu hệ thống đã có `/etc/immich-ha/config.json` hoặc cụm PostgreSQL `16/ha`, chuyển tới [Cập nhật hệ thống đã chạy](#12-cập-nhật-hệ-thống-đã-chạy). Không chạy lại bootstrap/stage.

## 1. Hai Immich và ổ dữ liệu

Môi trường đã kiểm chứng: Immich native 3.4.1 và nâng cấp theo recipe lên 3.4.2, PostgreSQL 16.16, user `immich`, Debian LXC amd64, dịch vụ Redis `redis-server.service`. Hai máy phải cùng kiến trúc, cùng bản Immich, cùng PostgreSQL 16 và các extension tương thích (đặc biệt vector/VectorChord). Bộ cài streaming hiện không hỗ trợ tự chuyển PostgreSQL major khác sang 16.

Có thể tạo LXC bằng [community-script Immich](https://community-scripts.github.io/ProxmoxVE/scripts?id=immich). Chạy lệnh upstream trên **Proxmox host**, không chạy bên trong một LXC đã có Immich:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/immich.sh)"
```

Script upstream có thể đổi phiên bản/package/layout. Xác nhận cấu hình thực tế; không mặc nhiên coi bản mới nhất tương thích với bộ tích hợp này.

Trên **mỗi LXC** kiểm tra:

```bash
hostname -I
psql --version
id immich
findmnt -T /mnt/immich-storage
mountpoint /mnt/immich-storage
df -hT / /mnt/immich-storage
grep '^IMMICH_MEDIA_LOCATION=' /opt/immich/.env
systemctl is-active postgresql redis-server
```

Media root chuẩn là **`/mnt/immich-storage`**, trong đó có `library/`, `upload/`, `thumbs/`, `encoded-video/`, `profile/`, `backups/`. Hai máy dùng cùng đường dẫn tuyệt đối vì database có thể lưu đường dẫn này. Không thêm nhầm một tầng `library/library`.

- Gắn ổ thật/volume Proxmox vào media root; không dùng một thư mục trống trên rootfs để thay mount.
- Dùng cấu hình mount bền vững để ổ xuất hiện sau reboot. Bind mount cần quyền UID/GID phù hợp với LXC unprivileged.
- Nếu đã có ảnh ở chỗ khác, hoàn tất di chuyển theo [tài liệu storage](https://docs.immich.app/administration/backup-and-restore/) trước khi bắt đầu HA; chỉ sửa `.env` không tự sửa đường dẫn trong database.
- Ban đầu chỉ `primary` chứa dữ liệu nguồn và được người dùng truy cập. `backup` là bản cài tương thích, không cho nhận upload độc lập.
- Source rootfs cần thêm tối thiểu 3 GiB trống để staging; ổ ảnh cần đủ chỗ cho toàn thư viện, versioning và archive vật lý.

**Đạt:** mount thật, phiên bản/extension khớp, media root đúng, xem được ảnh trên nguồn. Tạo một bản backup độc lập trước migration.

## 2. Tailscale bên trong hai LXC

Với unprivileged LXC, cấp `/dev/net/tun` từ **Proxmox host** theo [hướng dẫn Tailscale LXC](https://tailscale.com/docs/features/containers/lxc/lxc-unprivileged). Kiểm tra `pct config <ID>` và cấu hình hiện có trước khi thêm; không ghi đè toàn bộ features hoặc thêm mount TUN trùng lặp.

Trên **mỗi LXC**, cài theo [hướng dẫn Linux chính thức](https://tailscale.com/docs/install/linux), ví dụ:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
tailscale up --accept-dns=false
tailscale ip -4
ip -br addr show tailscale0
tailscale status
```

Đăng nhập cùng tailnet và đặt tên dễ phân biệt. Cần TUN/kernel networking để PostgreSQL bind IP Tailscale; không thay bằng userspace networking rồi mặc nhiên dùng cùng cấu hình bind.

Ghi IP **của LXC**, ví dụ `100.64.10.10` và `100.64.10.20`; không nhầm với IP tail của host. Từ mỗi LXC kiểm tra peer:

```bash
tailscale ping --c=1 --until-direct=false 100.64.10.20
```

Thay IP bằng peer thật. Policy/firewall cần cho hai LXC kết nối TCP `22000` (Syncthing) và `5433` (replication). Máy quản trị cần TCP `22` tới LXC nếu SSH qua tailnet. Không mở PostgreSQL ra Internet. Direct thường tốt hơn relay; kiểm tra đường thực tế, không suy đoán từ địa chỉ LAN/IPv6.

**Đạt:** có `tailscale0`, hai LXC liên lạc được, máy quản trị SSH tới đúng hai LXC. Kiểm tra fingerprint SSH bằng console độc lập rồi pin vào `known_hosts`; không tắt host-key checking.

## 3. Syncthing và đồng bộ file ban đầu

Nếu chưa có, trên **hai LXC**:

```bash
apt-get update
apt-get install -y syncthing
systemctl enable --now syncthing@immich.service
```

Giữ GUI ở loopback. Mở qua SSH từ **máy quản trị**, ví dụ hai cổng local khác nhau:

```bash
ssh -N -L 8385:127.0.0.1:8384 root@100.64.10.10
ssh -N -L 8386:127.0.0.1:8384 root@100.64.10.20
```

Đặt mật khẩu GUI. Kết nối hai Device ID; đặt địa chỉ peer `tcp://<IP-tail-peer>:22000`. Không chia sẻ API key hoặc config XML. Agent tự tìm config của instance `immich`; nếu dùng vị trí khác, điền `syncthing_config` trong node config sau khi xác minh.

Tạo hai folder với **cùng ID trên hai máy**:

| Folder ID | Đường dẫn trên cả hai LXC | Nguồn ban đầu | Dự phòng ban đầu |
| --- | --- | --- | --- |
| `immich-library` | `/mnt/immich-storage` | Send Only | Receive Only |
| `immich-database` | `/mnt/immich-storage/backups/postgres` | Send Only | Receive Only |

Tạo sẵn thư mục backup, bảo đảm user chạy Syncthing đọc/ghi được các thư mục cần thiết và user `immich` đọc/ghi được media. Với bind mount unprivileged cần xử lý UID/GID; không chạy `chmod -R 777` hoặc `chown -R` cả ổ một cách mù quáng. Đường dẫn nhận mặc định `~/Immich Library` phải đổi thành đúng media root trước khi đồng bộ.

Trong Ignore Patterns của **folder thư viện trên cả hai máy**, ban đầu đặt:

```text
/encoded-video/.immich
/library/.immich
/profile/.immich
/thumbs/.immich
/upload/.immich
/lost+found
/icloud-download
/backups
```

Folder `immich-database` là folder riêng nên vẫn truyền nội dung `backups/postgres`. Không chia sẻ `/var/lib/postgresql`, `backups/pg-stream` hoặc archive recovery. Sau khi hệ thống hoạt động, bước 9 sẽ mở riêng các file SQL backup trong gốc `backups/`, giữ các archive khác bị loại trừ. `.stignore` không tự truyền giữa hai máy; [quy tắc đầu tiên khớp quyết định](https://docs.syncthing.net/users/ignoring.html).

**Đạt:** hai folder đều đúng path, đúng Device ID, không còn lỗi quyền/marker, nguồn Send Only và đích Receive Only, ảnh/video đã đồng bộ. Không bấm Override/Revert khi chưa xác định rõ nguồn dữ liệu.

## 4. Cấu hình máy quản trị

Từ repo, tạo venv và file riêng như [README](../README.md). Dùng `.venv\Scripts\Activate.ps1` trên PowerShell hoặc `source .venv/bin/activate` trên Linux.

Trong `project.json` điền:

- `workerName`: tên deployment; giữ ổn định khi cập nhật.
- `publicHost`: ví dụ `photos.example.org`.
- `adminHost`: ví dụ `immich-ha.example.org`.
- Hai `origins`: ví dụ `https://immich-primary.example.org` và `https://immich-backup.example.org`.
- Metadata hai node: tên, IP thực sự có trên LXC, container ID.
- Access Team domain, Application AUD và email quản trị.

Trong `.env` điền account/token Cloudflare; SSH cho hai LXC; Client ID **và Client Secret** nếu origins được Access Service Auth bảo vệ. Xem [CONFIG.vi.md](CONFIG.vi.md). Máy quản trị phải có đường SSH tới cả hai LXC; dùng `SSH_JUMP_*` nếu cần một jump host chung.

## 5. Cloudflare Worker, domain và Access

### Bốn hostname, hai loại đích

| Hostname mẫu | Đích | Bảo vệ |
| --- | --- | --- |
| `photos.example.org` | **Worker Custom Domain** | Đăng nhập Immich; không áp trang Access tương tác gây chặn app |
| `immich-ha.example.org` | **Worker Custom Domain** | Access ứng dụng quản trị, email allowlist |
| `immich-primary.example.org` | Tunnel của LXC A → `http://127.0.0.1:8080` | Origin token/epoch; thêm Access Service Auth nếu dùng |
| `immich-backup.example.org` | Tunnel của LXC B → `http://127.0.0.1:8080` | Tương tự A |

`adminHost` không cần đưa vào Tunnel. Domain Immich công khai **không trỏ trực tiếp tới cổng 2283 của một máy**, vì như vậy sẽ bỏ qua điều phối.

### Access

Tạo application bảo vệ dashboard, lấy **Team domain** (`...cloudflareaccess.com`) và **Application AUD** của application đó vào `project.json`. Đây khác với Service Token. Policy email phải khớp `access.emails`.

Nếu có wildcard Access bao phủ tất cả subdomain, tạo cấu hình cụ thể cho các hostname này để app Immich và Worker không bị chuyển tới trang login. Để bảo vệ origins bằng Access, dùng policy **Service Auth → Include Service Token** và điền đủ Client ID/Secret vào `.env`; không thay bằng policy đăng nhập email. Truy cập origin trực tiếp bằng browser có thể trả 403 là đúng. Không bỏ gate chỉ để làm origin mở công khai.

### Deploy Worker

Trên **máy quản trị**, từ `cloudflare-ha/`:

```bash
python deploy.py
python admin.py recovery-off
```

Token Cloudflare phải có quyền Workers cần thiết trong đúng account. Công cụ tạo Worker/SQLite Durable Object, biến/secret và Cron, sinh dữ liệu riêng vào `generated/`. **Nó không tạo Access, Tunnel hoặc Custom Domain.** Không xóa `generated/` để chạy lại. Tắt recovery lúc cài để tự chuẩn bị dự phòng không chen vào các bước bootstrap thủ công; auto-failover OFF không đồng nghĩa auto-recovery cũng OFF.

Trong Cloudflare dashboard, gắn `publicHost` và `adminHost` vào Worker bằng **Custom Domains**. Xem [tài liệu Cloudflare](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/): hostname có CNAME cũ phải được xử lý trước; không xóa hostname Tunnel của hai origins.

Tạo hai Tunnel độc lập và cài `cloudflared` như hướng dẫn Cloudflare. Nếu dùng `127.0.0.1:8080`, connector phải chạy trong chính LXC có Nginx đó. Tạo Published Application cho từng origin như bảng. Không công khai `8384`, `9081` hoặc `5433` qua Tunnel.

## 6. Bootstrap agent ở chế độ quan sát

Chỉ làm trên LXC **chưa có** `/etc/immich-ha/config.json`. Copy vào một thư mục riêng của đúng LXC:

```text
node/install-node.sh
node/agent.py
node/standby-run
node/immich-ha.nginx.conf
generated/<node>/config.json   → đặt tên config.json cạnh install-node.sh
```

Config phải đúng node/IP và `enforce: false`. Không dùng nhầm config A cho B. Installer backup cũ chỉ được nối nếu đã có state legacy; LXC backup mới không cần bộ installer riêng theo IP cá nhân.

Trong **LXC A**:

```bash
bash install-node.sh primary
```

Trong **LXC B**:

```bash
bash install-node.sh backup
```

Kiểm tra cả hai:

```bash
nginx -t
systemctl is-active nginx immich-ha-agent
```

Trên **máy quản trị**:

```bash
python admin.py status --compact
python admin.py probe
python check-public.py
```

**Đạt:** heartbeat hai node xuất hiện, nguồn `primary` phục vụ; domain công khai trả ping và header HA của `primary`; auto còn OFF. Lỗi 403/Access ở origin cần sửa trước, không bật auto để chữa lỗi.

## 7. Runtime v2, enforcement và bootstrap dữ liệu

Trên **máy quản trị**:

```bash
python tools/package-nodes.py
python ssh_nodes.py primary --upgrade
python ssh_nodes.py backup --upgrade
```

Hai ZIP riêng chứa khóa, không được chia sẻ. Nếu cài thủ công, giải nén đúng ZIP vào đúng LXC và chạy `bash upgrade-v2.sh`. Script yêu cầu auto OFF, không có job/pending, mount và IP/khóa đúng. Nó chuyển sang `immich-ha-app.service`, tắt service Immich gốc và giữ file quay lui.

Copy `node/enable-enforcement.sh` vào **mỗi LXC**, chạy sau khi domain đi qua Worker và hai gates:

```bash
bash enable-enforcement.sh
```

Script hỗ trợ runtime v2 và bind app loopback. Agent phải báo enforcement hợp lệ; không còn cổng 2283 lắng nghe public/LAN. Chưa bật auto.

Chuẩn bị đích lần đầu bằng runtime v2 từ **máy quản trị**:

```bash
python admin.py prepare --target backup
python admin.py status --compact
```

Chờ job hoàn tất. Bước bootstrap này **còn dùng dump một lần** vì streaming chưa được cài. Nó xác nhận đường dẫn ảnh, thư viện và khả năng restore trước khi chuyển sang streaming. Không dùng các script standby cá nhân cũ. Nếu GUI API sẵn sàng nhưng ảnh thiếu hoặc đường dẫn sai, xử lý ở bước này.

**Đạt:** `ready: true`, `standbyEligible: true`, hai heartbeat mới, phiên bản phù hợp, job succeeded; cả hai Syncthing folder không pause/lỗi. Hai máy vẫn auto OFF.

## 8. PostgreSQL streaming và bàn giao WAL

Theo [STREAMING.vi.md](STREAMING.vi.md), trên **máy quản trị**:

```bash
python tools/streaming-upgrade.py stage
python tools/streaming-stage-test.py
python tools/streaming-cutover.py
python tools/streaming-upgrade.py status
python check-public.py
```

`stage` tạo riêng cluster `16/ha:5433`, giữ `16/main:5432`. `streaming-stage-test.py` chỉ dành cho cụm chưa enabled. `cutover` dừng ghi ngắn để chốt bản copy cuối, giữ rollback, rồi chuyển app sang `immich_stream`. Đây không phải installer lặp lại được tùy ý.

Nếu cutover đã bật streaming nhưng hết thời gian xác nhận, xem status/log; sau khi hai máy đã khỏe, `python tools/streaming-finalize.py` kiểm tra rồi khôi phục tùy chọn đã lưu. Nó không khắc phục một database lỗi bằng cách bỏ qua kiểm tra.

**Đạt:** cùng system identifier, standby `inRecovery: true`, `receiverConnected: true`, không còn WAL đã nhận chờ replay; app source đang dùng 5433; domain HTTP 200. Backend hiện tại báo `handoverVersion: 2`, `fastHandoverVersion: 1`. Standby không chạy app Immich, agent dùng health PostgreSQL/mount.

## 9. Backup SQL và ngừng dump định kỳ

Trong **Immich admin**, bật/xác nhận lịch backup SQL gốc và chính sách giữ mong muốn. Xác nhận file `.sql.gz` thực sự được tạo trong `/mnt/immich-storage/backups`, không chỉ mới lưu lịch.

Trên **máy quản trị**:

```bash
python tools/sync-native-backups.py
```

Tool sửa ignore có chọn lọc trên hai máy để truyền SQL backup theo role, không mở thư mục PGDATA/archive. Xác nhận file trên hai máy, đối chiếu checksum và thử restore vào database/cụm riêng khi cần.

Nếu chọn bỏ dump HA theo giờ, trên **cả hai LXC**:

```bash
touch /etc/immich-ha/disable-scheduled-dump
chmod 600 /etc/immich-ha/disable-scheduled-dump
systemctl disable --now immich-ha-dump.timer
```

Marker được installer/runtime tôn trọng. Không ảnh hưởng lịch SQL gốc Immich. Trong chế độ streaming mới, job bàn giao cũng không tạo dump. Các dump migration/legacy đã có không tự bị xóa.

Sau khi đã thử streaming, có backup hợp lệ và kiểm tra không còn client dùng cluster cũ, có thể đặt `/etc/postgresql/16/main/start.conf` thành `manual`, chạy `systemctl daemon-reload` rồi `pg_ctlcluster 16 main stop` để giảm RAM. Giữ data directory; không stop `postgresql.service` chung hoặc cluster `ha`. Không trỏ app trở về database cũ sau khi cụm mới đã nhận ghi.

## 10. Bật tự động và thử nghiệm

Trên **máy quản trị**:

```bash
python admin.py recovery-on
python admin.py preference-on --target primary
python admin.py auto-on
python admin.py status --compact
```

Recovery và tự trở về máy ưu tiên có công tắc riêng trên UI; Slack cấu hình tại đó. Tự chuyển khi sự cố không có công tắc tắt trên dashboard: dùng `auto-on` sau khi source/standby và gate được xác nhận, rồi giữ bật trong vận hành. Script bảo trì/rollout vẫn có thể tạm khóa tự động. `preference-on` là tùy chọn; bỏ qua nếu không muốn tự trở về một máy cố định. Luôn xem status trước thao tác.

Thử có kiểm soát: chuyển active chủ động A → B → A, rồi thử sự cố và máy cũ trở lại; upload/xem ảnh sau mỗi lần. Bàn giao nhanh chỉ rút thời gian fencing khi có chứng cứ dừng nguồn, không bỏ kiểm tra WAL/file hay thời gian startup. Ghi lại kết quả thật; không gọi test controller là test failover thực tế.

## 11. Witness, quorum và đường truy cập dự phòng

Phần này mở rộng hệ thống streaming đã khỏe/idle. Làm từng checkpoint trong [QUORUM.vi.md](QUORUM.vi.md), kiểm tra xong mới sang bước kế. Không paste cả quy trình như một script liên tục.

### Witness và kết nối

Tạo LXC Debian 13 amd64 trên máy vật lý thứ ba, độc lập với hai host Immich. Cấu hình đã dùng để kiểm tra: 1 vCPU, 1 GiB RAM, rootfs 8 GiB; không cần ổ ảnh hoặc Immich/PostgreSQL trên witness. Nó không cần cùng cluster Proxmox; quorum Immich khác QDevice Proxmox.

Cấp TUN từ host như bước 2, rồi cài SSH/Tailscale **bên trong witness**, đăng nhập cùng tailnet và kiểm tra kết nối tới hai LXC. Kiểm tra hostname trước khi cài để không làm nhầm trên host. Đồng bộ giờ; xác minh/pin SSH fingerprint bằng console. Điền `SSH_COORDINATOR_*` và key trong `.env`; tên coordinator ở đây chỉ là cấu hình SSH witness, không thay vai trò chính của Cloudflare.

| Kết nối | Phạm vi |
| --- | --- |
| TCP 22 | Máy quản trị/jump host tới ba LXC |
| TCP 2379, 2380 | Ba thành viên quorum qua Tailscale/mTLS, không public |
| TCP 5433, 22000 | Replication/Syncthing giữa hai Immich node |
| TCP 8090 mặc định | Client LAN/Tailscale tới gateway; witness tới gateway node |
| HTTPS outbound | Worker API, Tailscale, nguồn package khi triển khai |

### Quorum và fallback

Từ `cloudflare-ha/` trên máy quản trị, theo thứ tự:

| Bước | Lệnh | Điểm kiểm tra |
| --- | --- | --- |
| 1 | `python tools/quorum-preflight.py` | Danh tính, Tailscale/SSH, một writer và replica đúng lineage |
| 2 | `python tools/quorum-stage.py --install` | etcd riêng, mTLS, chưa cấp quyền active |
| 3 | `python tools/quorum-stage.py --check` | Ba member/endpoint khỏe |
| 4 | `python tools/quorum-shadow.py --deploy` | Observer/CAS/TTL shadow, chưa đổi grant |
| 5 | `python tools/quorum-shadow.py --check` | Quan sát đúng owner/epoch |
| 6 | `python tools/quorum-enforce.py --deploy` | Grant quorum bắt buộc |
| 7 | `python tools/quorum-enforce.py --check` | Marker/proof/lease và PostgreSQL đúng |
| 8 | `python tools/quorum-fallback.py --deploy` | Bật fallback và đối chiếu CF |
| 9 | `python tools/quorum-fallback.py --check` | Authority/quorum và agent hợp lệ |

Giữ cluster ID, CA và key trong `generated/`; không tạo lại khi lỗi. Test dừng quorum witness chỉ dành cho giai đoạn isolated. Bài chặn heartbeat CF là checkpoint riêng có timer phục hồi, không chạy trong mỗi lần triển khai. TLS compat chỉ dành cho cụm cũ thiếu AKI theo tài liệu, không chạy repair lên cụm mới vì thấy tên tool.

### Gateway và Funnel

| Bước | Lệnh triển khai | Người vận hành thử trước khi tiếp tục |
| --- | --- | --- |
| 1 | `python tools/install-local-gateway.py --deploy` | App qua LAN/Tailscale node active; standby đóng |
| 2 | `python tools/install-funnel.py --deploy` | URL riêng active: login/xem/upload qua 4G |
| 3 | `python tools/install-witness-gateway.py --deploy` | Gateway witness đúng owner/epoch |
| 4 | `python tools/install-witness-funnel.py --deploy` | URL chung qua Internet và giữ URL khi đổi active |
| 5 | `python tools/install-funnel-control.py --deploy` | ⚙ có URL/IP đúng, witness xác nhận Funnel đang bật |

Bốn tool đầu có `--check`. Tailscale có thể yêu cầu chủ tailnet phê duyệt HTTPS/Funnel qua link CLI; hoàn tất rồi tiếp tục. Tool giữ Serve hiện có, không reset toàn bộ route. Không public 2283, 9081, PostgreSQL hay etcd.

Bước điều khiển cần Funnel chung/riêng đã được xác nhận và Funnel chung đang bật. Nó bổ sung metadata URL/IP, thêm token riêng, cập nhật Worker và cài timer witness. Timer lấy yêu cầu khoảng 15 giây/lần, chỉ đổi đúng listener chung và báo xác nhận; không đổi role/grant hoặc service Immich. Tắt URL chung ngắt người đang dùng URL đó, giữ đường CF và Funnel riêng.

Chỉ muốn giám sát: đặt `funnelUrl` trong project rồi `python deploy.py --update`, không cần timer. Probe Internet mỗi phút dùng nonce tránh cache; kết quả quá 3 phút đánh dấu cũ. Khi CF mất, monitor/dashboard có thể mất; witness giữ Funnel hiện có. Xem [CONFIG.vi.md](CONFIG.vi.md).

### Nghiệm thu

Thử bàn giao chủ động, sự cố một LXC, máy đó trở lại và chuẩn bị dự phòng, tự trở về máy ưu tiên, URL Funnel chung, lỗi đường điều phối CF rồi khôi phục. Sau mỗi bước xác nhận xem/upload, một PostgreSQL writable, standby gate đóng, WAL/file đủ và job không lỗi. Tắt/bật Funnel là bài thử riêng do người vận hành chọn thời điểm.

Ghi rõ bài đã thử thật và bài chỉ mô phỏng. Chặn heartbeat agent không chứng minh mọi kiểu outage Cloudflare/network partition đã được kiểm tra.

## 12. Cập nhật hệ thống đã chạy

Không chạy lại `install-node.sh`, `configure.py`, `stage` hoặc `cutover`.

| Phạm vi | Lệnh trên máy quản trị |
| --- | --- |
| Worker/UI | `python deploy.py --update` |
| Agent streaming và bàn giao WAL/nhanh | `python tools/upgrade-wal-handover.py` |
| Runtime khi đã bật quorum/fallback | `python tools/quorum-fallback.py --deploy`, hai node khỏe/idle, theo checkpoint 3c; không chạy lại stage |
| Kiểm tra quorum/fallback | `python tools/quorum-fallback.py --check` |
| Cài/cập nhật điều khiển Funnel | `python tools/install-funnel-control.py --deploy`, Funnel chung đang bật và cấu hình cũ khớp |
| Chỉ build ZIP node | `python tools/package-nodes.py` |
| Kiểm tra public | `python check-public.py` |
| Cài tính năng nâng cấp Immich trên dashboard | `python tools/upgrade-immich-ha.py --via primary --deploy` |

Nâng phiên bản ứng dụng Immich là quy trình riêng: đọc [IMMICH-UPGRADE.vi.md](IMMICH-UPGRADE.vi.md). Deploy feature không tự nâng Immich. Trên UI, chuẩn bị staging trước, sau đó xác nhận áp dụng cho cả hai máy; không dùng updater gốc trực tiếp với các service/database HA.

Rollout runtime yêu cầu hệ thống khỏe/idle, giữ module cũ và tạm tắt tự động, cập nhật executable agent **và** module Python, sau đó mới cập nhật Worker và khôi phục tự động. Nếu kiểm tra thất bại, xem status; không tự bật auto khi chưa đủ điều kiện. `.env`, `project.json`, `generated/`, node IDs và Durable Object storage ID phải được giữ nguyên.

## Khi có lỗi

| Hiện tượng | Kiểm tra trước |
| --- | --- |
| Origin mở trực tiếp báo 403 | Có thể đúng: Access Service Auth/gate đang chặn. Dùng `admin.py probe` để kiểm tra request Worker có token |
| Domain Immich không vào được sau xóa Tunnel cũ | Kiểm tra publicHost có Worker Custom Domain; đừng gắn lại publicHost trực tiếp vào LXC |
| Agent không enforce | `.env` có đúng một `IMMICH_HOST=127.0.0.1`, socket 2283 chỉ loopback, module/executable đồng bộ |
| Thiếu `.immich` / quyền file | Kiểm tra đúng mount, UID/GID và marker; không tạo marker trên mount bị mất |
| Syncthing stopped/marker missing | Kiểm tra folder path, mount và `.stfolder`; không reset index để che lỗi sai đường dẫn |
| SSH bị từ chối host key | Xác minh fingerprint qua console và cập nhật pin đúng máy; không dùng AutoAddPolicy để bỏ kiểm tra |
| Replica mất nguồn | Tailscale/ACL, PostgreSQL 5433, `receiverConnected`; không promote nếu seed/WAL chưa xác nhận |
| Job lỗi sau chốt ghi | Giữ đóng nguồn, đọc job/log; chỉ dùng Resume đúng owner/epoch khi đủ điều kiện |

Xem [runbook vận hành](DASHBOARD.vi.md) để lấy log. Các hostname, IP và ID trong tài liệu là ví dụ; thay trong cấu hình riêng, không hardcode vào source.
