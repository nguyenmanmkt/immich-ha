# Chuẩn bị Cloudflare

Làm trên trình duyệt trước `init`. Không tạo Worker, Tunnel hoặc Access application bằng tay; script sẽ tạo chúng qua API.

## 1. Tài khoản và domain

Trong [Cloudflare Dashboard](https://dash.cloudflare.com/):

1. Thêm domain, cấu hình nameserver và chờ trạng thái **Active**.
2. Vào **Workers & Pages**, hoàn tất đăng ký và kiểm tra đã có `<account>.workers.dev`.
3. Mở **Zero Trust**, hoàn tất tạo team/gói/điều khoản nếu được yêu cầu.

Chuẩn bị tên chưa có DNS/Worker/Tunnel/Access app trùng:

| Giá trị | Ví dụ |
| --- | --- |
| Worker | `immich-ha` |
| Domain Immich | `immich.example.com` |
| Domain dashboard | `immich-ha.example.com` |
| Email quản trị | `you@example.com` |

Script tự dùng `immich-origin-a.example.com` và `immich-origin-b.example.com`. Không tạo DNS trước hoặc xóa tài nguyên đang dùng để chạy lại init.

## 2. Lấy ID

Chọn domain → **Overview → API** gần cuối trang, copy **Account ID** và **Zone ID**; mỗi ID có 32 ký tự hexadecimal. Hai ID phải thuộc cùng account/zone. Tham khảo [cách lấy ID](https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/).

## 3. Tạo API token

**My Profile → API Tokens → Create Token → Create Custom Token**. Dùng user API token, không dùng Global API Key, Tunnel token hoặc Access Client Secret.

| Phạm vi | Permission | Mức |
| --- | --- | --- |
| Account | Workers Scripts | Edit |
| Account | Cloudflare Tunnel | Edit |
| Account | Access: Apps and Policies | Edit |
| Account | Access: Service Tokens | Edit |
| Account | Access: Organizations, Identity Providers, and Groups | Edit |
| Zone | Zone | Read |
| Zone | DNS | Edit |

- **Account Resources:** Include → Specific account → chọn đúng account.
- **Zone Resources:** Include → Specific zone → chọn domain.
- Tạo token, copy và giữ riêng; dán vào wizard A khi được hỏi.
- Nếu giới hạn IP nguồn, cho phép IP public của A, không phải IP LAN/Tailscale.

Một số giao diện tách quyền Organizations và Identity Providers; chọn Edit cho cả hai thay nhóm chung. Tunnel có thể mang tên Cloudflare One Connector: cloudflared/Cloudflare One Connectors. API gọi quyền Edit là Write. Workers Scripts đã bao gồm thao tác Custom Domain/Cron của bộ cài.

Hướng dẫn chính thức: [tạo API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/).

## 4. Chạy init/join và đăng nhập

Tiếp tục [DEPLOYMENT.vi.md](DEPLOYMENT.vi.md). Wizard nhận **Account ID, Zone ID, token, domain và email**, rồi tạo Worker/Durable Object, Access OTP, service credential và hai Tunnel.

| Địa chỉ | Dùng cho |
| --- | --- |
| `https://<worker>.<account>.workers.dev` | Agent và join; không chặn bằng Access login tương tác |
| Domain dashboard | Trình duyệt → email → mã OTP |
| Domain Immich | App/web → tài khoản Immich hiện có |
| Hai origin | Tunnel → Nginx `127.0.0.1:8080`, bảo vệ bằng Service Auth/gate |

Domain dashboard/Immich là Worker Custom Domain, không gắn thẳng Tunnel tới cổng 2283. Không nhập API token để đăng nhập dashboard.

## Lỗi thường gặp

| Lỗi | Kiểm tra |
| --- | --- |
| API 401/403 | Token, quyền và resource scope; verify thành công chưa chứng minh đủ mọi quyền |
| Join gặp trang login | Dùng Controller URL workers.dev, không dùng domain dashboard |
| Không nhận OTP | Email/policy, thư rác và các Access/WAF rules có sẵn |
| Init tạo dở tài nguyên | Giữ `.install/init.json`; không xóa state hoặc chạy lại từ đầu tùy tiện |
