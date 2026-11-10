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
