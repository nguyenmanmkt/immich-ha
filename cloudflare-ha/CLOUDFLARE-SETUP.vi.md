# Chuẩn bị Cloudflare trước khi chạy init

Làm phần này trên trình duyệt. Chưa cần tạo Worker, Tunnel hay trang quản lý bằng tay: `bash install.sh init` sẽ tạo chúng qua API.

## 1. Domain và tài khoản

1. Đăng nhập [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Thêm domain vào đúng tài khoản Cloudflare, cấu hình nameserver và đợi domain có trạng thái **Active**.
3. Chọn tên dự kiến, ví dụ domain của bạn là `example.com`:

| Giá trị | Ví dụ | Cách sử dụng |
| --- | --- | --- |
| Worker name | `immich-ha` | Wizard hỏi khi init |
| Domain Immich | `immich.example.com` | App iPhone/web đăng nhập Immich |
| Domain quản lý | `immich-ha.example.com` | Dashboard HA, đăng nhập qua Access |
| Origin A | `immich-origin-a.example.com` | Script tự chọn theo zone |
| Origin B | `immich-origin-b.example.com` | Script tự chọn theo zone |
| Email quản trị | `you@example.com` | Nhận mã đăng nhập dashboard |

Không tạo DNS cho bốn hostname này trước. Installer hiện từ chối DNS, Worker, Tunnel hoặc Access app trùng tên thay vì tiếp quản chúng. Nếu đã có triển khai, không xóa tài nguyên đang dùng để chạy lại init.

## 2. Bật workers.dev và Zero Trust

1. Vào **Workers & Pages**, hoàn tất đăng ký Workers nếu tài khoản chưa dùng. Kiểm tra đã có account subdomain dạng `<ten-tai-khoan>.workers.dev`; giao diện có mục **Your subdomain** để quản lý.
2. Mở **Zero Trust**, hoàn tất bước tạo team/chọn gói và điều khoản nếu được yêu cầu. Bộ cài dùng Access để bảo vệ trang quản lý; không cần WARP trên điện thoại để đăng nhập trang này.
3. Không cần tự tạo một Worker rỗng hoặc Access application. Installer lấy Team domain/AUD và tạo policy sau đó.

Controller URL sẽ có dạng `https://immich-ha.<ten-tai-khoan>.workers.dev`. Nó dùng cho agent và join; domain quản lý dành cho trình duyệt. Không chặn toàn bộ workers.dev controller bằng Access yêu cầu đăng nhập tương tác, vì agent cần gửi heartbeat HTTPS.

Tham khảo: [workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/).

## 3. Lấy Account ID và Zone ID

Trong Cloudflare, chọn domain → **Overview** → tìm phần **API** gần cuối trang, sao chép **Account ID** và **Zone ID**. Mỗi giá trị là chuỗi 32 ký tự hexadecimal, không phải tên tài khoản hoặc tên domain.

Cũng có thể tìm Account ID bằng ô tìm kiếm Cloudflare với lệnh **Copy account ID**. Hai ID phải thuộc cùng tài khoản/zone đã chọn.

Hướng dẫn chính thức: [Find account and zone IDs](https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/).

## 4. Tạo API token cho bộ cài

Dùng **user API token** cho luồng hiện tại; code kiểm tra qua `/user/tokens/verify`. Không dùng Global API Key, Tunnel token hoặc Access Client Secret thay thế.

1. Avatar tài khoản → **My Profile → API Tokens → Create Token**.
2. Chọn **Create Custom Token / Get started**; đặt tên ví dụ `immich-ha-install`.
3. Thêm các quyền bên dưới. Giao diện thường ghi **Edit**, còn tài liệu API gọi cùng nhóm quyền là **Write**.

| Phạm vi | Permission | Mức | Dùng để |
| --- | --- | --- | --- |
| Account | Workers Scripts | Edit | Upload Worker, Durable Object binding/migration, secrets, workers.dev và Cron |
| Account | Cloudflare Tunnel | Edit | Tạo hai Tunnel, cấu hình ingress và lấy connector token |
| Account | Access: Apps and Policies | Edit | Tạo application/policy dashboard, app và origin |
| Account | Access: Service Tokens | Edit | Tạo credential để Worker truy cập origin |
| Account | Access: Organizations, Identity Providers, and Groups | Edit | Đọc/cấu hình Zero Trust organization và email OTP provider |
| Zone | Zone | Read | Kiểm tra domain Active và Account ID |
| Zone | DNS | Edit | Kiểm tra/tạo CNAME cho hai origin |

Nếu giao diện tách quyền Access chi tiết, nhóm Organizations/Identity Providers có thể được thay bằng **Access: Organizations — Edit** và **Access: Identity Providers — Edit**. Tunnel có thể xuất hiện dưới tên mới **Cloudflare One Connector: cloudflared** hoặc **Cloudflare One Connectors**. Chọn nhóm có quyền ghi tương ứng, không cần thêm tất cả các tên thay thế.

4. **Account Resources → Include → Specific account**: chọn tài khoản chứa domain.
5. **Zone Resources → Include → Specific zone**: chọn domain triển khai.
6. Nếu đặt bộ lọc IP nguồn, phải cho phép IP public nơi LXC A gọi API; IP LAN/Tailscale không phải IP public của request này.
7. Chọn **Continue to summary → Create Token**. Sao chép token ngay khi hiển thị và lưu riêng; khi wizard A hỏi **Cloudflare API token**, dán giá trị này. Không đưa vào repo hoặc ảnh chụp.

Worker Custom Domains dùng quyền Workers Scripts Write trong API hiện tại. Bộ cài không tạo Workers Route nên không yêu cầu thêm quyền Workers Routes cho luồng này.

Nguồn đối chiếu: [tạo token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/), [Worker Custom Domains API](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/update/), [Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/), [Zero Trust organization](https://developers.cloudflare.com/api/resources/zero_trust/subresources/organizations/methods/create/), [identity provider](https://developers.cloudflare.com/api/resources/zero_trust/subresources/identity_providers/methods/create/), [service token](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/service_tokens/methods/create/), [Tunnel](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/create/).

## 5. Quay lại cài đặt trên A/B

Bạn đã có: **Account ID + Zone ID + API token + hai domain + email**, và workers.dev/Zero Trust đã sẵn sàng. Tiếp tục [DEPLOYMENT.vi.md — Cài hai node](DEPLOYMENT.vi.md#2-cài-hai-node).

| Bạn làm trước | Script init/join tự làm |
| --- | --- |
| Cài sẵn Immich, mount ổ và kết nối mạng A/B | Cài agent, Nginx gate và Syncthing |
| Chuẩn bị domain/tài khoản/API token | Upload Worker, tạo Durable Object, Cron và Custom Domains |
| Chọn email quản trị | Tạo Access email OTP, application và policy |
| Bật workers.dev | Tạo Controller URL, credential riêng và mã join |
| Chạy init trên A, join trên B | Tạo hai Tunnel và CNAME origin; chạy connector trong từng LXC |
| Theo dõi và nghiệm thu | Đồng bộ media, thiết lập PostgreSQL streaming |

Dashboard chạy trên Worker, không cần cài một web server quản lý riêng trên máy thứ ba. Domain dashboard trỏ Worker Custom Domain; **không gắn nó vào Tunnel tới cổng 2283**. Hai Tunnel chỉ dẫn origin tới Nginx gate `http://127.0.0.1:8080` trong từng LXC, không đi thẳng Immich.

## 6. Đăng nhập và kiểm tra

1. Mở domain quản lý đã nhập, ví dụ `https://immich-ha.example.com`.
2. Nhập đúng email quản trị, nhận mã OTP và đăng nhập. Không cần nhập API token vào trang đăng nhập.
3. Xem thẻ thiết lập cụm; đợi A/B sẵn sàng rồi thử domain Immich.
4. Domain Immich dùng tài khoản Immich hiện có; hai origin có Service Auth, nên truy cập origin trực tiếp không phải bài kiểm tra đăng nhập app.

Nếu Cloudflare trả **401/403 khi init**, kiểm tra token còn hiệu lực, đúng Account/Zone Resources và quyền của endpoint được báo. Token verify thành công chỉ chứng minh token hợp lệ, không chứng minh đủ mọi quyền cài đặt. Nếu init đã tạo một phần tài nguyên rồi lỗi, giữ `.install/init.json` để xử lý checkpoint; không xóa tài nguyên hoặc chạy lại từ đầu tùy tiện.

Nếu dashboard không gửi OTP, kiểm tra email/policy và thư rác. Nếu join gặp trang Access login, dùng Controller URL workers.dev do A in ra thay vì domain dashboard. Với tài khoản có wildcard Access application/WAF riêng, kiểm tra rule hiện có có chặn controller/origin không; không bypass toàn bộ zone.
