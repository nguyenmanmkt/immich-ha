# Lớp điều phối dự phòng khi Cloudflare gián đoạn

**Cài mới sau init/join:** dùng [WITNESS-SETUP.vi.md](WITNESS-SETUP.vi.md), gồm `witness-init`/`witness-join`, LAN hoặc Tailscale, không SSH. Phần CLI bên dưới là runbook triển khai cũ/bảo trì chuyên biệt; không chạy nối tiếp để cài lại một cụm mới đã ghép witness. Kiến trúc, giới hạn và các checkpoint trong tài liệu này vẫn là tham chiếu cho bản live cũ.

Mục tiêu đã chốt: Cloudflare điều phối chính; ba thành viên `primary`, `backup`, `witness` giữ khóa phối hợp theo đa số 2/3. Witness ở máy vật lý độc lập với hai Immich. Funnel chỉ bổ sung đường truy cập công khai, không tự cấp quyền active. IP LAN/Tailscale vẫn cần gateway kiểm tra grant và đăng nhập Immich.

Triển khai theo checkpoint: isolated → shadow 3a → khóa bắt buộc 3b → fallback 3c → gateway/Funnel 4a–4d → giám sát/điều khiển 4e. Cài observer chưa bật enforcement/fallback; bật fallback chưa tự cài đường truy cập thay thế. Cloudflare vẫn điều phối chính. Tổng quan tại [ARCHITECTURE.vi.md](ARCHITECTURE.vi.md), thứ tự end-to-end tại [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md).

## Bước 1 — Kiểm tra ba LXC

Trên máy quản trị:

```bash
cd cloudflare-ha
python tools/quorum-preflight.py
```

Điền `SSH_COORDINATOR_HOST`, `SSH_COORDINATOR_USER` và mật khẩu hoặc private key trong `.env`. Có thể dùng `SSH_KEY_PATH` / `SSH_KEY_PASSPHRASE` chung khi chưa đặt key riêng cho witness. Host key của cả ba máy phải được xác minh và pin trước; không dùng AutoAddPolicy.

CLI SSH vào primary bằng transport hiện tại, tìm IP peer từ cấu hình streaming thật để vào backup qua primary; SSH witness trực tiếp hoặc qua primary. Không lấy IP private từ source hardcode. Kiểm tra root/Debian 13, định danh LXC, TUN, IP Tailscale riêng, RAM/disk, cổng 2379/2380 đang trống, sáu chiều kết nối Tailscale và một PostgreSQL writable duy nhất với replica cùng system identifier đang nhận WAL. Nó không chạy installer hoặc thao tác SQL ghi.

Kết quả riêng nằm trong `generated/quorum-preflight.json` và `generated/quorum-topology.json`. Topology có cluster ID ổn định, cờ grant/Funnel đều false. Chạy lại không đổi cluster ID; khác topology sẽ dừng để tránh restage nhầm. Không publish các file này.

App chạy bằng user `immich` phải đọc được marker `/etc/immich-ha/quorum-required` khi khởi động. Bộ cài 3b/3c đặt riêng thư mục `/etc/immich-ha` thành `root:immich 0710`: chỉ cho phép đi qua tới marker, không cho liệt kê thư mục. Các file cấu hình/khóa vẫn `root:root 0600`; bộ cài từ chối mở quyền nếu phát hiện file riêng tư có thể bị đọc. Không dùng `chmod -R` hoặc bỏ kiểm tra grant để xử lý lỗi quyền.

## Bước 2 — Cài quorum riêng, chưa cấp quyền active

Trên máy quản trị:

```bash
python tools/quorum-stage.py
python tools/quorum-stage.py --install
python tools/quorum-stage.py --check
```

Lệnh đầu chỉ kiểm tra topology. `--check` kiểm tra cụm đã cài và key thử riêng, không upload/restart dịch vụ. `--install` tải etcd **3.6.17** từ release chính thức, kiểm tra SHA-256 đã ghim rồi cài binary vào `/opt/immich-ha-quorum/v3.6.17/`. Không dùng dịch vụ etcd mặc định của hệ điều hành. Dịch vụ riêng là `immich-ha-quorum.service`, user `immich-quorum`, data `/var/lib/immich-ha-quorum/`, cấu hình `/etc/immich-ha-quorum/`.

Cổng client 2379 và peer 2380 chỉ bind IP Tailscale. Hai CA riêng cho peer và client; các kết nối cần chứng thư đã ký, probe client không được dùng làm peer. CA private key chỉ lưu trong `generated/quorum-pki/` trên máy quản trị, không upload lên LXC. Không tự tạo lại key khi chạy lại; PKI/cluster ID hoặc file đã cài khác checksum sẽ dừng để kiểm tra, không restage PGDATA hay data etcd.

Heartbeat etcd là 200 ms, election timeout 2 giây cho các đường Tailscale khác site. Giới hạn dịch vụ: RAM 256 MiB, CPU 50% một core, quota database 128 MiB; auto compaction 1 giờ. Đây là cấu hình cho dữ liệu điều phối nhỏ. Xem [hướng dẫn tuning](https://etcd.io/docs/v3.6/tuning/).

Tool kiểm tra ba voting member cùng cluster/leader, endpoint health qua chứng thư, từ chối client không có chứng thư, rồi ghi/đọc/xóa một key ngẫu nhiên duy nhất dưới `/immich-ha/<clusterId>/checks/`. Kết quả lưu riêng vào `generated/quorum-stage-report.json`. Không ghi khóa owner/lease/epoch, không điều khiển Immich/PostgreSQL, không bật Funnel. Nếu cài dở, giữ nguyên file để điều tra; không xóa data để cài lại.

Trong witness có thể kiểm tra:

```bash
systemctl status immich-ha-quorum.service --no-pager
journalctl -u immich-ha-quorum.service -n 30 --no-pager
```

Bài test dừng một thành viên là checkpoint riêng sau khi ba endpoint khỏe; chưa được thực hiện tự động bởi installer.

### Checkpoint: dừng quorum trên witness

Sau khi người vận hành đã sẵn sàng, trên máy quản trị:

```bash
python tools/quorum-test-witness.py
```

Tool chỉ áp dụng khi cụm còn isolated, ba endpoint đang khỏe và Immich HA đang idle/ready. Nó đặt timer systemd tự bật lại quorum witness sau 120 giây trước khi dừng dịch vụ, kiểm tra witness không còn nghe cổng client, rồi xác nhận hai thành viên còn lại giữ được leader và ghi/đọc key dưới namespace `checks/`. Không loại witness khỏi membership, không tắt LXC hoặc dừng Immich. Sau bài test, witness được bật lại trong `finally`, key thử được xóa và cả ba endpoint được kiểm tra lại. Nếu mất kết nối máy quản trị, timer phía witness vẫn thực hiện phục hồi; không cancel timer trước khi đã xác nhận service active.

Kết quả riêng lưu `generated/quorum-witness-test.json`, kèm owner/epoch/auto sau test. Bài kiểm tra này chứng minh cụm etcd chịu mất thành viên witness; nó **chưa** chứng minh Immich chịu được mất Cloudflare vì chưa tích hợp cấp lease dự phòng.

## Checkpoint 3a — Quan sát khóa song song

```bash
python tools/quorum-shadow.py --deploy
python tools/quorum-shadow.py --check
```

`--deploy` lưu bản agent trước cập nhật, tạm tắt automation trong lúc reload agent rồi khôi phục tùy chọn cũ sau khi xác nhận source/standby khỏe và owner/epoch giữ nguyên. Không restart ứng dụng Immich hoặc PostgreSQL. Có lỗi thì automation giữ OFF; bản lựa chọn trước rollout nằm riêng trong `generated/quorum-shadow-rollout-*.json`. Không chạy lại vội khi chưa kiểm tra trạng thái.

Module `node/quorum_lock.py` chỉ chấp nhận `mode=shadow`, dùng API etcd qua mTLS và read/transaction tuyến tính; CAS so sánh revision của cả generation và active key trong một giao dịch. Lease gắn TTL native của etcd; khi hết TTL, generation vẫn giữ để bác bỏ thông tin cũ. Thử nghiệm CF/fallback dùng scope `test-<uuid>` riêng, sau đó xóa đúng hai key thử. Observer thật ghi scope `admission` trong `/immich-ha/<clusterId>/shadow/`; không có key nào trong namespace cấp quyền thực tế.

Agent nguồn quan sát đề xuất lease Cloudflare, standby chỉ đọc. Thread riêng, hàng đợi giới hạn một mẫu mới nhất, timeout mạng có giới hạn. Lỗi observer không gọi activate/fence, không chặn heartbeat; telemetry `quorumShadow` chỉ là chẩn đoán. Worker vẫn quyết định owner/epoch/lease như trước. Không có daemon promote dự phòng được cài ở checkpoint này.

Các bài test: đề xuất đối nghịch khi lease còn giữ phải bị từ chối; hết lease thử thì thế hệ mới mới được nhận; Cloudflare thế hệ cũ không được ghi đè; CAS đồng thời chỉ một phía được nhận; giao dịch mất phản hồi không được báo nhận khóa khi chưa xác minh. Kết quả thật lưu `generated/quorum-shadow-report.json`. Đây là bằng chứng cho logic shadow, chưa phải test failover ứng dụng khi mất Cloudflare.

### Tương thích TLS của cụm cũ

Chứng thư mới có SKI/AKI để đáp ứng Python/OpenSSL strict verification. Etcd JSON gateway dùng leaf `probe.crt/key` có `clientAuth` cho kết nối gRPC nội bộ; `server.crt` vẫn chỉ có `serverAuth`. File client key chỉ root và group dịch vụ `immich-quorum` đọc được. Không tắt xác minh TLS hoặc client certificate authentication.

Với cụm isolated đã cài từ profile cũ, cập nhật rõ ràng trước khi bật observer:

```bash
python tools/quorum-tls-compat.py --apply
```

Tool ký lại các leaf client/server thiếu AKI bằng **CA/private key cũ**, giữ subject/public key/validity, giữ nguyên peer cert, bổ sung credential cho JSON gateway. Nó đối chiếu fingerprint đã cài, cập nhật bản ghi cài đặt, restart từng thành viên và chờ ba endpoint khỏe trước khi sang máy kế. Không tạo cụm mới, không xóa data etcd/PGDATA. Hồ sơ thay đổi riêng nằm trong `generated/quorum-cert-compat-changes.json`.

## Checkpoint 3b — Khóa quorum bắt buộc

```bash
python tools/quorum-enforce.py --deploy
python tools/quorum-enforce.py --check
```

Sau khi enforcement đã khỏe, cập nhật code của checkpoint này bằng `python tools/quorum-enforce.py --update`. Lệnh giữ nguyên marker bắt buộc và config hiện có; không dùng để tự sửa một rollout còn dang dở hoặc để bật fallback.

Checkpoint này yêu cầu cả hai LXC đang idle, cùng PostgreSQL system identifier, một nguồn ghi và một replica đang nhận WAL. Tool giữ owner/epoch/session, lưu bản file cũ cùng lựa chọn automation, tạm tắt auto/recovery/preference rồi cài code trên ba máy. Nó đánh dấu topology là đang cutover trước khi bật marker bắt buộc trên witness, standby và cuối cùng active. Không tạo lại cụm, không khởi động lại ứng dụng hoặc PostgreSQL. Chỉ restart agent; automation cũ được khôi phục sau khi cả hai máy đã có marker, active có grant mới và WAL/health vẫn đạt.

`node/quorum_authority.py` sử dụng namespace thật `/immich-ha/<clusterId>/control/admission/`, tách khỏi shadow. Cloudflare vẫn đề xuất owner/epoch/lease; agent chỉ được activate sau khi CAS quorum chấp thuận. Hai đường Cloudflare/fallback dùng cùng giao thức khóa, không được giành một lease native đang còn hiệu lực; generation bền vững bác bỏ epoch/session cũ cả sau TTL. **Chưa có daemon fallback được cài/chạy ở checkpoint 3b** (`fallbackEnabled=false`); mất Cloudflare lâu vẫn hết lease và đóng phục vụ. Không coi bước này là hoàn thành khả năng chịu mất Cloudflare.

Kiểm tra quorum có ở trước promote, mở app, resume và khởi động bản nâng cấp; guard của tác vụ kiểm tra generation chung. Grant cục bộ bị chặn bởi cả lease CF và thời hạn quorum có margin 30 giây, đo bằng Linux `CLOCK_BOOTTIME` để tính cả lúc host suspend. Origin kiểm tra grant mỗi request; thread fencing riêng kiểm tra mỗi giây, độc lập với heartbeat/job. App startup cũng cần proof của đúng cluster và boot hiện tại trong `/run/`. Credential mTLS vẫn chỉ root/dịch vụ quorum đọc được.

Bàn giao nhanh giữ kiểm tra WAL/file hiện tại. Chỉ sau bằng chứng `frozen` đã fsync, app/ML inactive, PostgreSQL dừng và file cấp quyền stream đã bị xóa, source mới CAS nhả khóa. Generation được giữ với tombstone `released`: heartbeat thường và agent reboot không thể mở lại epoch đó. Chỉ lệnh `resume` rõ ràng cho đúng owner/epoch/session từ coordinator mới mở lại; epoch mới có thể nhận quyền sau bằng chứng cũ đã dừng. Không nhả khóa sớm khi lỗi không rõ nguyên nhân.

Tool kiểm tra thật rằng cả đề xuất Cloudflare lẫn fallback đối nghịch đều bị lease active từ chối; bài kiểm tra này không promote, không dừng dịch vụ, không ngắt Cloudflare. Báo cáo riêng ở `generated/quorum-enforcement-rollout-*.json`. Tests offline bao phủ CAS, TTL, tombstone/resume, grant lỗi, đường promote/start và chặn origin; chúng không thay thế bài test partition/failover ứng dụng thực tế.

Nếu rollout lỗi: giữ automation OFF, giữ topology/marker/file backup để kiểm tra, chạy `--check`. Không xóa `quorum-required`, không copy lại agent cũ đang bỏ qua quorum, không chạy lại installer isolated/TLS/fault test. Muốn hạ lớp enforcement phải có quy trình fencing và drain riêng. Marker `grantIntegrationEnabled=true` chặn các tool staging cũ. `--check` dùng được sau cutover; các tool cài/thử isolated không được dùng nữa.

Quorum kiểm soát cấp quyền, không phải STONITH phần cứng. Origin đóng trên request mới khi grant hết hạn; fencing tiến trình vẫn phụ thuộc kernel/systemd có thể thực thi. Không dùng kết quả test lease để tuyên bố chịu được mọi kiểu kernel treo hoặc máy bị đóng băng.

## Checkpoint 3c — Điều phối dự phòng và đối chiếu khi Cloudflare hồi phục

```bash
python tools/quorum-fallback.py --deploy
python tools/quorum-fallback.py --check
```

Tool yêu cầu 3b đã enforced/khỏe và hai máy cùng lineage, idle. Nó lưu tùy chọn trước rollout, tạm tắt automation, cài code mới trước khi bật protocol CF, rồi bật `fallbackEnabled=true` lần lượt trên standby và active. Worker được cập nhật tại chỗ, giữ DO/storage ID và các khóa. Cụm etcd không được restage, PostgreSQL/Immich không bị restart bởi installer. Nếu DO đang trì hoãn cập nhật code, tool chờ protocol 2; không reset DO. Sau khi cả hai agent xác nhận capability, khôi phục tùy chọn cũ và chờ policy được ghi vào quorum. Báo cáo riêng: `generated/quorum-fallback-rollout-*.json`.

Logic dự phòng chạy trong hai agent đã có singleton; witness vẫn chỉ cung cấp một thành viên etcd, không chạy thêm một bộ cấp lease độc lập. Mất heartbeat CF do lỗi mạng/timeout/429/5xx: active chờ grace 15 giây rồi có thể CAS chuyển authority của **chính owner/epoch/session đó** sang fallback khi vẫn giữ native lease, máy khỏe, mount đúng và không có thao tác đang khóa. Mỗi vòng fallback gia hạn native TTL 180 giây; quyền cục bộ ngắn hơn TTL 30 giây và vẫn có watchdog/gate/startup checks. Đây không phải bỏ fencing khi CF hết lease: quyền đang chạy đã được cấp bởi quorum.

Máy khác chỉ được promote khi native lease thực sự mất, tiếp tục vắng mặt 300 giây, policy cho auto, máy không bị loại vì bảo trì, runtime xác nhận đúng seed/source epoch, WAL đã replay hết và còn đủ dung lượng. Syncthing thư viện phải `receiveonly`, không pause/lỗi, trạng thái idle, `needTotalItems=0`, `needBytes=0`. Tổng thời gian có thể khoảng 8–10 phút tùy lease còn lại; không hạ thời gian bằng cách bỏ bằng chứng. Streaming và file sync vẫn bất đồng bộ: kiểm tra này xác nhận trạng thái đã biết ở replica, không bảo đảm không mất giao dịch/file vừa phát sinh trên nguồn trước sự cố.

Policy CF có sequence tăng bền vững. Phản hồi cũ không thể ghi đè lựa chọn mới; standby cũng có thể chuyển policy mới vào record khi authority còn CF. Pending/job/nâng cấp khóa fallback; trước khi nguồn ghi mode `frozen`/`upgrade-frozen`, agent ghi `localBlocked` vào quorum. Sau khi nhả khóa bền vững, tombstone vẫn chặn tự tiếp quản. Không tiếp tục các lệnh CF cũ trong authority fallback/handoff; sau đối chiếu, job đã bị hủy cũng không thể tiếp tục chỉ vì owner/epoch trùng.

Khi CF hồi phục, agent đọc lại record tuyến tính rồi báo qua **agent token sẵn có**. CF dừng advance/job khi thấy fallback, gửi nonce cho agent đang thực sự giữ quyền. Agent phải CAS nonce vào native lease của cùng writer (`handoff`), gửi lại, rồi CF nhận đúng owner/epoch/session đó. Nếu CF đã tự tăng epoch mà chưa được quorum chấp nhận, bridge chỉ tăng epoch của cùng writer, không quay lại epoch thấp. CF chỉ mở lại automation sau heartbeat xác nhận grant CF mới có nonce đúng. Một term riêng phân biệt các chu kỳ: báo cáo peer bị trễ từ chu kỳ trước không thể kéo UI về đối chiếu lần nữa. Không đổi active chỉ vì CF vừa hồi phục; các lựa chọn failback/recovery cũ chỉ chạy lại khi đủ điều kiện bình thường.

CF tin các agent đã xác thực bằng token của từng máy để báo kết quả đọc quorum mTLS; CF không truy cập trực tiếp Tailscale/etcd và không tự kiểm chứng một chứng thư CA qua Internet. Hai LXC/witness/root keys thuộc cùng miền tin cậy; đây không phải giao thức Byzantine chịu được máy quản trị bị xâm nhập. Không đưa etcd hoặc agent token ra Funnel.

Trong outage, fallback giữ/phát quyền và có thể promote một replica đã chuẩn bị; nó không tự rebuild máy hỏng, chạy nâng cấp hoặc tự tái seed một timeline cũ. Việc chuẩn bị lại replica chờ CF trở lại hoặc thao tác khôi phục được kiểm soát. Mất đa số hoặc lineage/file chưa đạt thì đóng/quyền mới bị từ chối. Không xóa marker để “mở tạm”. Nếu rollout đang dang dở, giữ file/report và kiểm tra thủ công trước khi chạy tiếp.

Installer không tự gây outage/failover. Kiểm thử offline bao phủ giữ writer, TTL/confirmation, policy/block, WAL/media, nonce handback, stale epoch/term/job và mất quorum. Bài test thật chặn đường heartbeat CF, xác nhận native fallback rồi khôi phục/đối chiếu phải được thực hiện như checkpoint riêng; ghi rõ kết quả thực tế, không thay bằng kết quả unit test.

## Kiểm thử lỗi điều phối có kiểm soát

### Checkpoint: chặn riêng heartbeat rồi khôi phục

Khi người vận hành đã sẵn sàng theo dõi:

```bash
python tools/quorum-test-cloudflare.py --run
```

Tool cập nhật hook thử có giới hạn trên agent, đợi cả hai agent nhận grant bình thường, sau đó tạo marker root-only `0600` trong `/run/` để **không gửi POST heartbeat** và đi vào cùng nhánh lỗi kết nối. Không chặn HTTPS toàn máy, không sửa DNS/hosts/firewall, không tác động Tunnel hoặc các request CF khác. Đây là fault injection trên agent thật, không phải gây outage hạ tầng Cloudflare thật.

Marker gắn boot ID và `CLOCK_BOOTTIME`, hiệu lực tối đa 180 giây; marker sai owner/quyền, symlink, sai boot hoặc quá hạn bị bỏ qua. Timer systemd dọn marker được đặt trước khi bật lỗi; hết hạn marker vẫn tự vô hiệu kể cả khi timer không chạy. Sau khi quan sát fallback nhận khóa và ít nhất một lần gia hạn, tool luôn bỏ marker trong `finally`, rồi kiểm tra nonce handback về CF. Không tắt enforcement hoặc sửa owner để ép bài test qua.

Thành công yêu cầu owner/epoch giữ nguyên, PID app/PostgreSQL hai máy không đổi, nguồn vẫn ping được, standby giữ recovery, heartbeat được mở lại và auto/recovery/preference giữ lựa chọn ban đầu. Báo cáo riêng tại `generated/quorum-cloudflare-test-*.json`. Test này chưa chứng minh chịu mất domain/Tunnel, lỗi cả CF, failover sang máy khác hoặc gateway/Funnel; các trường hợp đó có checkpoint riêng.
## Checkpoint 4a — Gateway LAN/Tailscale

```bash
python tools/install-local-gateway.py
python tools/install-local-gateway.py --deploy
python tools/install-local-gateway.py --check
```

Lệnh đầu là preflight chỉ đọc: lấy IP eth0/Tailscale thật, kiểm tra port trống và Nginx hiện tại hợp lệ. Port mặc định `8090`, có thể chọn bằng `--port`; port app/origin/agent/PostgreSQL/etcd bị từ chối. Không đổi port của gateway đã cài bằng cách ghi đè marker. Installer yêu cầu hai node ready/idle dưới CF, giữ lựa chọn automation trong báo cáo riêng, lưu file trước thay đổi, cập nhật agent rồi reload Nginx; không restart Immich/PostgreSQL hoặc thay cổng loopback 2283.

Nginx thêm server riêng trong `/etc/nginx/conf.d/immich-ha-local.conf`, chỉ nhận client loopback, RFC1918, Tailscale CGNAT và IPv6 ULA. File thực có origin credential, quyền root-only `0600`; template source không chứa credential. Không tự sửa firewall, DNS, Tunnel hoặc mở port trên router. URL HTTP này dành cho LAN tin cậy hoặc mạng Tailscale đã mã hóa; HTTPS Funnel là checkpoint riêng.

Mỗi request app phải qua `auth_request` tới `/authorize-local` của agent loopback. Nginx tự chèn credential server-side; không tin token/epoch từ client. Agent chỉ cho phép khi enforce bật, mount có thật, role/ready/mode active và grant CF/quorum còn hiệu lực. Cloudflare tiếp tục dùng endpoint cũ bắt buộc epoch; local gateway không mở agent hoặc controller API ra LAN. Tài khoản/session/API key Immich vẫn do Immich kiểm tra như bình thường.

Standby, frozen, grant lỗi hoặc agent không phản hồi trả `503`, không proxy tới Immich. Header giả không mở được standby. Hỗ trợ streaming upload, WebSocket upgrade và timeout dài. Kết nối WebSocket đã mở vẫn theo cơ chế fencing tiến trình khi grant hết hạn, không kiểm tra grant trên từng frame. Giữ nguyên giới hạn cooperative fencing đã nêu ở 3b.

Installer kiểm tra thực từ witness qua IP Tailscale: active `/api/server/ping` trả pong, standby `503`, internal auth/control URL không công khai (`404`), header giả không đổi quyết định. So sánh PID app/PG và recovery role trước/sau, khôi phục automation sau khi kiểm tra qua. IP LAN có thể đổi do DHCP; lấy URL mới bằng preflight, không hardcode trong source. Báo cáo riêng `generated/local-gateway-rollout-*.json`. Người vận hành thử đăng nhập, xem và upload trên app trước khi sang Funnel.

## Checkpoint 4b — URL Funnel dự phòng

Sau khi người vận hành xác nhận đăng nhập/xem/upload qua 4a:

```bash
python tools/install-funnel.py
python tools/install-funnel.py --deploy
python tools/install-funnel.py --check
```

Preflight đọc phiên bản, DNSName, cấu hình Serve và marker gateway thật. URL lấy từ Tailscale, không hardcode hostname/domain trong source. Mặc định HTTPS 443; `--port` chỉ chấp nhận các port Funnel hỗ trợ 443/8443/10000. Tool dừng nếu selected port đang chạy listener khác, root route khác, hostname khác trên cùng port hoặc foreground Serve session. Các dịch vụ ở port khác phải giữ nguyên sau cập nhật; không dùng `serve reset`/`funnel reset`.

Mỗi LXC chạy `tailscale funnel --bg --yes --https=443 http://127.0.0.1:<gateway-port>`. Chỉ expose gateway có grant, không proxy thẳng Immich 2283, origin 8080, agent 9081, PostgreSQL hay etcd. Cấu hình background được Tailscale duy trì qua reboot/restart; không thêm một proxy tự cấp grant. Tài khoản/session Immich vẫn bắt buộc cho dữ liệu riêng. URL standby trả 503 cho API app đến khi node được cấp quyền thật.

Nếu tailnet chưa phê duyệt Funnel/HTTPS, CLI có thể trả link `login.tailscale.com/f/funnel?...`; chủ tailnet cần đăng nhập và phê duyệt tại link đó rồi chạy lại. Lệnh có timeout, không để một CLI chờ phê duyệt vô hạn. Cấu hình/snapshot riêng được giữ để đối chiếu, không tự reset để thử lại. Kiểm tra URL thực, không kết luận thành công chỉ vì CLI đã ghi cấu hình.

Installer lưu Serve trước/sau và output CLI ở thư mục root-only trên node, báo cáo riêng `generated/funnel-rollout-*.json` trên máy quản trị. Xác nhận public DNS bằng DNS-over-HTTPS, rồi kết nối **IP công khai** với SNI/certificate đúng hostname: tránh nhầm truy cập qua MagicDNS/Tailscale riêng là Funnel Internet đã hoạt động. Kiểm tra pong active, 503 standby, API tài khoản trả 401/403 khi chưa đăng nhập, internal/control URL trả 404; PID app/PG và owner/epoch phải giữ nguyên. Không đổi cài đặt tự động hoặc restart app/PG khi bật Funnel.

Hai URL gắn với danh tính node, không gắn cứng vai trò. Khi active đổi máy, URL của máy đó mới phục vụ; URL chung tự route được bổ sung riêng ở 4c/4d. Có thể giữ cả hai URL node làm đường trực tiếp khi witness lỗi. Domain Cloudflare vẫn là đường chính. Funnel dùng hạ tầng Tailscale và có giới hạn băng thông; không bảo đảm thay thế toàn bộ hạ tầng Internet khi nhiều nhà cung cấp cùng lỗi. Xem [Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel) và [yêu cầu/giới hạn](https://tailscale.com/docs/features/tailscale-funnel).

Sau khi cấp URL, người vận hành thử đăng nhập/xem/upload từ thiết bị tắt Tailscale (ví dụ 4G) để xác nhận trải nghiệm Internet. Bài test heartbeat 3c không phải test outage domain/Funnel; các checkpoint mất đường chính hoặc failover vẫn cần thử riêng.

## Checkpoint 4c — Gateway chung trên witness (LAN/Tailscale trước)

```bash
python tools/install-witness-gateway.py
python tools/install-witness-gateway.py --deploy
python tools/install-witness-gateway.py --check
```

Gateway chung được cài **bên trong LXC witness**, không trên Proxmox host. Port mặc định 8090, chọn riêng bằng `--port`. Preflight đối chiếu định danh/cụm, port trống, file đã quản lý và Nginx hiện có; không ghi đè một config lạ. Nếu witness chưa có Nginx, tool cài package và bỏ symlink site mặc định do lần cài mới tạo. Các site Nginx có sẵn được giữ. App/PG/agent của hai máy và dịch vụ etcd không restart; hai node chỉ reload Nginx để tin `X-Forwarded-Proto: https` từ đúng IP Tailscale của witness. Danh sách trusted gateway được lưu trong marker và giữ qua cập nhật local gateway sau này.

`immich-ha-router.service` chạy user riêng `immich-router`, code công khai trong `/usr/local/lib/immich-ha-router/`, socket Unix trong `/run/immich-ha-router/` chỉ cho Nginx đọc. Cấu hình và chứng thư được cấp bằng `LoadCredential`; không nới quyền thư mục code/credential quorum cũ. Module `node/witness_router.py` chỉ gửi transaction gồm hai `request_range` tuyến tính cho generation/active record; không có API cấp lease, ghi record, promote hay đổi owner. Nó dùng chứng thư client witness hiện có: đây là giới hạn ở code/service, không phải một role etcd RBAC mới. Không thay auth/RBAC cụm đang chạy chỉ để cài proxy.

Nginx chọn target bằng map hai node đã xác thực từ topology, không nhận upstream URL/IP từ client. Record phải cùng generation, có native lease và owner/epoch/session/authority hợp lệ; released/localBlocked thì không route. Chấp nhận authority CF/fallback/handoff của cùng cơ chế 3c. Không xác nhận được quorum thì 503, không dò máy nào trả ping để đoán active. Gộp request và cache tối đa 0,5 giây, tính từ lúc bắt đầu đọc bằng `CLOCK_BOOTTIME`; sau lỗi refresh không phục vụ dữ liệu stale. Origin gateway từng node vẫn kiểm tra grant riêng trước khi Immich xử lý request, kể cả lúc snapshot router vừa thay đổi.

Hỗ trợ HTTP upload streaming, Range và WebSocket. `proxy_next_upstream off` ngăn tự gửi lại POST/upload sang máy khác khi lỗi giữa chừng. WebSocket đã mở gắn với máy đã chọn cho đến khi ngắt; reconnect đọc route mới. Đăng nhập/API key vẫn do Immich xác thực. Các đường nội bộ/control không xuất hiện trên gateway. Header trả lời `X-Immich-HA-Node`/`Epoch` giúp kiểm tra route, không phải credential.

Installer xác nhận ping qua witness về owner/epoch hiện tại, endpoint tài khoản từ chối người chưa đăng nhập, đường auth/control trả 404, PID app/PG và cài đặt điều phối giữ nguyên. Tests offline bao phủ đổi owner, lease mất/frozen/record sai, cache hết hạn/lỗi refresh và request đồng thời. Đây chưa phải một bài bàn giao active thật. Có thể kiểm tra riêng dừng router mới với timer tự phục hồi: gateway phải 503 rồi về 200; không dừng etcd để thử proxy. Báo cáo riêng ở `generated/witness-gateway-rollout-*.json`.

Người vận hành thử login/xem/upload qua URL LAN/Tailscale witness trước. Bước này chưa bật Funnel chung và không đổi hai URL Funnel trực tiếp đã có.

## Checkpoint 4d — Funnel chung qua witness

Sau khi người vận hành xác nhận login/xem/upload qua 4c:

```bash
python tools/install-witness-funnel.py
python tools/install-witness-funnel.py --deploy
python tools/install-witness-funnel.py --check
```

Funnel witness trỏ `127.0.0.1:<witness-gateway-port>`, không trỏ vào một node cố định. Hostname lấy từ Tailscale, không hardcode domain; `--port` hỗ trợ các port Funnel 443/8443/10000. Tool kiểm tra hash gateway, owner/epoch từ response LAN, Serve hiện có và khả năng CLI trước khi cấu hình background. Không reset Serve, không sửa policy/role tự động, không restart router, Nginx, etcd, Immich hoặc PostgreSQL. Các URL node trực tiếp giữ nguyên.

Xác nhận qua IP Internet công khai với TLS/SNI đúng hostname: ping phải về owner/epoch hiện tại qua header của witness, API tài khoản trả 401/403 nếu chưa login, internal/control path trả 404. So sánh PID và role database hai node trước/sau. Snapshot CLI/Serve của witness được lưu root-only; báo cáo riêng `generated/witness-funnel-rollout-*.json`. Cờ `witnessFunnel` trong topology riêng chỉ ghi thành công khi public check qua.

Một URL công khai chung theo owner trong quorum; chuyển active không yêu cầu đổi URL trên client. Bài test bật Funnel chỉ chứng minh public ingress tới owner hiện tại, không tự thực hiện bàn giao active hoặc gây outage CF. Người vận hành thử login/xem/upload bằng 4G và tắt Tailscale trên client, sau đó mới chọn checkpoint đổi active/mất đường chính.

Witness là một phụ thuộc của đường chung, không thay Cloudflare đang điều phối chính; nếu witness/proxy lỗi thì đường chung đóng, các đường node trực tiếp và đường CF có thể vẫn hoạt động theo grant. Không có active hợp lệ hoặc không đọc được quorum thì không tự chọn một máy có ping để phục vụ.
## Checkpoint 4e — Dashboard và công tắc Funnel chung

Sau khi Funnel chung/riêng đã được xác nhận, hai Immich node khỏe/idle và Funnel chung đang bật:

```bash
python tools/install-funnel-control.py --deploy
```

Tool đọc URL/IP trong topology đã kiểm chứng, bổ sung metadata project, thêm `FUNNEL_CONTROL_TOKEN` chuyên dụng vào bộ secret riêng (giữ token cũ), cập nhật Worker và cài `immich-ha-funnel-control.service`/`.timer` trên witness. Timer chạy khoảng 15 giây/lần, xác nhận hostname/port/target trước khi sửa đúng listener chung và báo revision/trạng thái. Không reset Serve, đổi Funnel node, grant, database hay service Immich. Cấu hình witness root-only ở `/etc/immich-ha/witness-funnel-control.json`.

Công tắc lưu yêu cầu riêng với state điều phối; UI chỉ xác nhận khi witness báo đúng revision và trạng thái. Lỗi áp dụng/report cũ không được coi là thành công. Khi mất liên lạc Cloudflare, witness giữ Funnel hiện có. ⚙ hiển thị URL chung/riêng và IP Tailscale từ project. URL riêng không tự chuyển máy và chỉ phục vụ ở node active.

Probe Internet mỗi phút dùng nonce tránh cache owner cũ, kiểm tra ping và header node/epoch. Kết quả quá 3 phút được đánh dấu cũ. Nó không đổi role khi Funnel lỗi. Tắt URL chung có thể ngắt phiên đang dùng; kiểm thử công tắc là checkpoint riêng, không được suy ra từ việc cài timer thành công. Xem [CONFIG.vi.md](CONFIG.vi.md) và [DASHBOARD.vi.md](DASHBOARD.vi.md).

## Nghiệm thu và giới hạn

Kiểm thử có kiểm soát từng tình huống: chỉ mất domain, mất điều phối Cloudflare, một LXC lỗi, witness lỗi, partition thiểu số, mất đa số, reboot và hồi phục. Mỗi bước cần checkpoint với người vận hành; không dùng test unit để thay kết quả failover thực tế.

Cụm etcd 3 thành viên chịu được mất 1 thành viên nếu hai thành viên còn lại vẫn liên lạc được; mất đa số thì không được cấp quyền mới. Xem [etcd failure modes](https://etcd.io/docs/v3.6/op-guide/failures/) và [TLS client/peer authentication](https://etcd.io/docs/v3.6/op-guide/security/). Etcd chỉ giữ dữ liệu điều phối, không đồng bộ PGDATA hay thư viện ảnh. Quorum này là lớp mới, không tái sử dụng phiếu QDevice của cluster Proxmox.
