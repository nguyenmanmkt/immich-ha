# Kiểm tra và push GitHub

Repo: [nguyenmanmkt/immich-ha](https://github.com/nguyenmanmkt/immich-ha). Thực hiện từ checkout hiện tại; không tạo lại repo hoặc remote.

## Kiểm tra source

Trong `cloudflare-ha/`:

```bash
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.mjs
python tools/embed-ui.py
python tools/prepare-github.py
```

## Commit và push

Từ thư mục gốc repo:

```bash
git status --short
git diff --stat
git add .
git diff --cached
python cloudflare-ha/tools/prepare-github.py
git commit -m "Update installation and operations documentation"
git push
```

Đọc staged diff và chỉ commit khi audit không có findings. Audit đọc working tree, không thay việc kiểm tra nội dung đã stage; không tự commit/push hoặc deploy.

## File riêng

Không commit `.env`, `project.json`, `.install/`, `generated/`, host keys, token, SQL/physical backups hoặc ZIP node. Không dùng `git add -f` để đưa chúng vào repo. Nếu secret từng nằm trong lịch sử Git, thêm `.gitignore` không xóa được secret đó.

Tạo ZIP source khi cần:

```bash
python cloudflare-ha/tools/prepare-github.py --zip
```

Kết quả: `cloudflare-ha/generated/github-source.zip`, khác ZIP node chứa credential. CI chỉ kiểm tra source, không tự deploy production. Giấy phép/tài sản: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
