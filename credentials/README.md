# YouTube & External Credentials Directory

Thư mục lưu trữ các file định danh ứng dụng OAuth 2.0 Client Secrets từ Google Cloud Console.

> [!WARNING]
> Tất cả các file JSON chứa secret trong thư mục này được cấu hình loại trừ (`.gitignore`) để bảo mật, tuyệt đối không commit lên Git.

---

## 1. Danh Sách Credential Hiện Có

| Tên File | Google Cloud Project ID | Loại Ứng Dụng (Type) | Ghi Chú & Kênh Sử Dụng |
| :--- | :--- | :--- | :--- |
| `client_secrets_adroit.json` | `adroit-particle-495719-f2` | **Desktop app** (`installed`) | Credential cũ, đang dùng cho kênh **"Đầu Tư Nhàn Rỗi Và Thông Minhh"** (`default`). |
| `client_secrets_skilled.json` | `skilled-orbit-514306-e1` | **Desktop app** (`installed`) | Credential mới chuẩn Desktop App, hỗ trợ xác thực redirect localhost động mượt mà. |

---

## 2. Hướng Dẫn Sử Dụng Khi Đăng Ký Kênh Mới

### Sử dụng Credential Desktop App (Khuyên dùng):
```bash
python -m src.cli.main youtube accounts --account-action add --account <ten_kenh> --secrets credentials/client_secrets_adroit.json
```

### Sử dụng Credential Skilled Orbit:
```bash
python -m src.cli.main youtube accounts --account-action add --account <ten_kenh> --secrets credentials/client_secrets_skilled.json
```

---

## 3. Phân Biệt: Desktop App vs Web Application trên Google Cloud

- **Desktop app (Khuyên dùng cho Tool/CLI):** Cho phép OAuth 2.0 loopback server (`http://localhost:<random_port>/`) tự động mà không cần khai báo trước port. Rất phù hợp với script Python trên máy tính cá nhân.
- **Web application:** Bắt buộc phải khai báo chính xác từng URL và Port trong mục **Authorized redirect URIs** trên Google Cloud Console. Nếu không khai báo, Google sẽ từ chối với mã lỗi `Error 400: redirect_uri_mismatch`.

## Kaggle: nhiều token, fallback tuần tự

Registry cục bộ: `credentials/kaggle_accounts.json` (Git ignore, quyền `0600`).
Có thể đổi vị trí bằng `KAGGLE_ACCOUNTS_FILE`. Registry chỉ lưu tên/token và
account mặc định; không lưu quota, trạng thái session, cooldown hay blacklist.
Token cũ được giữ khi thêm account đầu tiên. Sau đó registry là danh sách chính;
`remove` thực sự bỏ token khỏi các lần thử sau.

```bash
python -m src.cli.main cloud accounts --json
python -m src.cli.main cloud accounts --account-action add --account backup --json
python -m src.cli.main cloud accounts --account-action default --account backup --json
python -m src.cli.main cloud accounts --account-action remove --account backup --json
```

`add` hỏi token qua prompt ẩn; dùng `--token-file <private-file>` để nhập từ file.
`list` chỉ hiển thị tên và account mặc định, không hiển thị token.

Mỗi job thử account hiện tại rồi lần lượt token còn lại, tối đa một lần/token
trong lần dispatch đó. Account không xác thực được, lỗi API/quota/session,
submission trả `False`/`error` hoặc job cloud thất bại thì chuyển token.
Thành công thì dừng; hết danh sách thì báo lỗi và dừng. Không truy vấn quota hay
số session trước khi chạy. Lỗi setup, approval, input/artifact validation vẫn
bị chặn ngay. Token không được đưa vào payload, worker hoặc metadata.

Áp dụng cho chapter, series, các lệnh TTS/render/batch dùng KaggleCloudService,
và dispatcher benchmark hiện có. Không chạy benchmark khi chỉ quản lý account.
`KaggleCloudService(api_token=...)` giữ nguyên ý nghĩa chỉ dùng token chỉ định.
