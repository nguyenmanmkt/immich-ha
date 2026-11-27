# Bảo trì hệ thống đang chạy

Không chạy lại `init/join`, `configure.py`, stage hoặc cutover trên cụm HA đã hoạt động. Giữ nguyên node ID, Worker name, Durable Object storage ID, khóa và database hiện có.

## Worker và UI

Trong checkout đang giữ cấu hình thật của deployment, từ `cloudflare-ha/`:

```bash
python deploy.py --update
```

Lệnh giữ state/khóa, cập nhật Worker/UI và biến cấu hình. Không cài agent, không tạo lại Tunnel/DNS/Access và không nâng phiên bản Immich. Sau cập nhật, kiểm tra dashboard và xem/upload qua domain chính.

## Agent và tính năng của bản cài cũ

Các lệnh sau dùng **hồ sơ triển khai SSH cũ**: `.env`, host key đã pin và `generated/` tương ứng. Không dùng như bước nối tiếp init/join mới. Chỉ rollout khi hai node khỏe, cùng lineage và không có job/nâng cấp đang chạy.

| Phạm vi | Lệnh trong `cloudflare-ha/` |
| --- | --- |
| Runtime đã bật quorum/fallback | `python tools/quorum-fallback.py --deploy` |
| Kiểm tra quorum/fallback | `python tools/quorum-fallback.py --check` |
| Runtime streaming chưa bật quorum | `python tools/upgrade-wal-handover.py` |
| Tính năng nâng cấp Immich | `python tools/upgrade-immich-ha.py --via primary --deploy` |
| Điều khiển Funnel cũ đã cài và đang bật | `python tools/install-funnel-control.py --deploy` |

Tool giữ bản trước rollout, tạm dừng automation và chỉ khôi phục sau kiểm tra. Có lỗi thì giữ automation tắt, đọc report/log; không ép bật lại hoặc bỏ marker fencing. Bộ cài init/join mới chưa có luồng cập nhật agent tự động riêng đã nghiệm thu.

Nâng ứng dụng Immich thực hiện trên [dashboard Nâng cấp](IMMICH-UPGRADE.vi.md), không chạy updater community-script trực tiếp.

## Chẩn đoán

| Hiện tượng | Kiểm tra |
| --- | --- |
| Domain không vào được | Worker Custom Domain, Tunnel, Access origin; không trỏ domain chính thẳng LXC |
| Origin trực tiếp trả 403 | Có thể đúng do Service Auth/gate; kiểm tra qua Worker |
| Syncthing marker missing | Mount, folder path, `.stfolder`; không reset index để che sai đường dẫn |
| Replica mất kết nối | IP peer, firewall, listener 5433 và WAL |
| Gateway/Funnel trả 503 | Grant, quorum và trạng thái chuyển máy; không start app standby thủ công |
| Lỗi sau dừng ghi | Giữ fencing, đọc job/log; không trỏ app về database cũ |

Log và thao tác thường ngày: [DASHBOARD.vi.md](DASHBOARD.vi.md). Đổi domain/IP: [CONFIG.vi.md](CONFIG.vi.md).
