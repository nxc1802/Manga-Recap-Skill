#!/usr/bin/env python3
"""
scripts/youtube_auth.py
Interactive script to authenticate and generate/renew YouTube OAuth2 token (.youtube_token.json).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli.youtube_cmd import YOUTUBE_SCOPES, get_youtube_oauth_credentials
from src.youtube.account_manager import account_manager


def main():
    parser = argparse.ArgumentParser(description="Authenticate and refresh YouTube OAuth2 multi-account token.")
    parser.add_argument("--account", default="default", help="Account identifier alias (e.g. 'main', 'ch2', 'review_truyen')")
    parser.add_argument("--set-default", action="store_true", help="Set this account as the default upload account")
    parser.add_argument("--force", action="store_true", help="Force re-authentication by removing old token")
    parser.add_argument("--secrets", default="client_secrets.json", help="Path to client_secrets.json")
    parser.add_argument("--token", default=None, help="Custom path to save token JSON (default: managed in data/youtube_accounts/tokens/<account>.json)")
    args = parser.parse_args()

    account_name = args.account.strip()
    secrets_path = Path(args.secrets)
    if not secrets_path.is_absolute():
        secrets_path = PROJECT_ROOT / secrets_path

    if args.token:
        token_path = Path(args.token)
        if not token_path.is_absolute():
            token_path = PROJECT_ROOT / token_path
    else:
        token_path = account_manager.tokens_dir / f"{account_name}.json"

    os.environ["YOUTUBE_CLIENT_SECRETS_FILE"] = str(secrets_path)
    os.environ["YOUTUBE_CREDENTIALS_FILE"] = str(token_path)

    print("=" * 60)
    print("  YouTube OAuth 2.0 Multi-Account Authenticator")
    print("=" * 60)
    print(f"👤 Account Alias      : \033[1;36m{account_name}\033[0m")
    print(f"📁 Client Secrets File: {secrets_path}")
    print(f"🔑 Output Token File  : {token_path}")

    if not secrets_path.exists():
        print("\n❌ LỖI: Không tìm thấy file client_secrets.json!")
        print("Vui lòng tải file OAuth 2.0 Client ID (Desktop Application) từ Google Cloud Console:")
        print("1. Truy cập https://console.cloud.google.com/")
        print("2. Vào APIs & Services > Credentials > OAuth 2.0 Client IDs")
        print("3. Tải file JSON về và lưu tên thành 'client_secrets.json' tại thư mục gốc của dự án.")
        sys.exit(1)

    if args.force and token_path.exists():
        print(f"\n🔄 Xóa token cũ theo yêu cầu (--force): {token_path}")
        token_path.unlink()

    print("\n⏳ Đang khởi tạo luồng xác thực OAuth2...")
    try:
        acc = account_manager.add_or_update_account(
            account_name=account_name,
            client_secrets_file=secrets_path,
            token_file=token_path if token_path.exists() else None,
            set_default=args.set_default,
            force_auth=args.force,
        )
        creds, _ = account_manager.get_credentials(account_name=account_name)
    except Exception as e:
        print(f"\n❌ Xác thực thất bại: {e}")
        sys.exit(1)

    # Test token via YouTube API
    print("\n🔍 Kiểm tra kết nối tới YouTube Data API...")
    try:
        c_title, c_id = account_manager.fetch_channel_info(creds)
        if c_title:
            print(f"✅ Đăng nhập thành công kênh: \033[1;32m{c_title}\033[0m (ID: {c_id})")
        else:
            print("✅ Xác thực thành công (nhưng tài khoản chưa tạo kênh YouTube hoặc không có quyền).")
    except Exception as e:
        print(f"⚠️ Cảnh báo khi test API YouTube: {e}")

    reg = account_manager.load_registry()
    is_def = (account_name == reg.default_account)
    print(f"\n🎉 Tài khoản '{account_name}' đã được lưu thành công tại: {token_path}")
    print(f"⭐ Trạng thái mặc định: {'Có (Default)' if is_def else 'Không'}")
    if getattr(creds, "expiry", None):
        print(f"⏰ Hạn sử dụng Access Token: {creds.expiry} (Refresh Token sẽ tự động làm mới)")
    print("=" * 60)


if __name__ == "__main__":
    main()
