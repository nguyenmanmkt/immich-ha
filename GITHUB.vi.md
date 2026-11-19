# Chuẩn bị source để đưa lên GitHub

Repo dự kiến ở thư mục chứa README.md này; source ứng dụng nằm trong cloudflare-ha/.

## File được chia sẻ

Source Worker, web, agent, tests, tài liệu hiện tại, cấu hình mẫu và workflow kiểm tra. project.json, .env, wrangler.jsonc, generated/, private keys, database dumps, ZIP node và ghi chép migration riêng không được đưa lên.

Kiểm tra hoặc tạo ZIP source sạch:

```bash
cd cloudflare-ha
python tools/prepare-github.py
python tools/prepare-github.py --zip
```

ZIP kết quả nằm trong generated/github-source.zip. Đây là ZIP source công khai; khác hoàn toàn primary-v2.zip / backup-v2.zip chứa token riêng. Công cụ kiểm tra các file sẽ chia sẻ, tìm secret đã lưu cục bộ và một số định dạng secret phổ biến. Không thay thế kiểm tra thủ công hoặc secret scanning của GitHub.

## Tạo repo và commit

Từ thư mục chứa README.md:

```bash
git init
git add .
git status --short
git diff --cached --stat
git diff --cached
```

Xem staged diff trước khi commit. Không dùng git add -f cho file bị bỏ qua.

```bash
git commit -m "Initial Immich HA source"
```

Sau đó tạo repo GitHub, chọn private/public và thiết lập remote theo URL repo của bạn. Chưa có lệnh push hoặc tạo repo nào được công cụ audit thực hiện.

## Trước khi public

- Chủ dự án chọn LICENSE nếu muốn cấp một giấy phép cụ thể.
- Kiểm tra lại quyền sử dụng logo/tài sản bên thứ ba theo THIRD_PARTY_NOTICES.md.
- Không tạo workflow auto-deploy bằng token production trong lần push đầu. Workflow đi kèm chỉ test, không liên lạc Cloudflare/LXC và không cần secret.
- Nếu token từng bị commit trước đó, bỏ file hoặc thêm .gitignore không xóa token khỏi lịch sử; phải xử lý token và lịch sử riêng.
