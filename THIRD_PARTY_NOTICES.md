# Tài sản và nguồn tham khảo bên thứ ba

- Logo Immich trong `cloudflare-ha/web/admin.js` lấy từ [Immich](https://github.com/immich-app/immich/blob/main/design/immich-logo.svg), theo [giấy phép dự án nguồn](https://github.com/immich-app/immich/blob/main/LICENSE).
- Tên và nhãn hiệu Immich, Cloudflare, Syncthing, Tailscale thuộc các chủ sở hữu tương ứng. Repo này là tích hợp HA độc lập, không phải sản phẩm chính thức của các dự án đó.
- File HTML tham khảo thiết kế trong `cloudflare-ha/docs/reference/` không được phục vụ bởi Worker production. UI production dùng `web/index.html`, `web/admin.css`, `web/admin.js` và `web/status-model.mjs`.

Chưa gán giấy phép cho toàn bộ mã của repo này; quyết định phát hành thuộc chủ dự án.

## Native build recipe

Các bước build trong `cloudflare-ha/node/build-immich-release.sh` được điều chỉnh từ [community-scripts/ProxmoxVE/ct/immich.sh](https://github.com/community-scripts/ProxmoxVE/blob/main/ct/immich.sh), tác giả vhsdream, giấy phép MIT. Bản tích hợp này bỏ điều khiển service, apt và database khỏi script build. Thông báo dưới đây áp dụng cho phần upstream được sử dụng, không gán giấy phép chung cho toàn repo.

Copyright (c) 2021-2026 community-scripts ORG

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
