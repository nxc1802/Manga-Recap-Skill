@echo off
:: ==============================================================================
:: Manga Recap Pipeline Orchestrator - Windows Setup Script
:: Installs from pyproject.toml
:: ==============================================================================
chcp 65001 >nul
echo === Khởi động Script Thiết lập Hệ thống Manga Recap ===

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [Lỗi] Python chưa được cài đặt hoặc chưa cấu hình PATH.
    exit /b 1
)

where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [Cảnh báo] FFmpeg chưa được cài đặt hoặc chưa cấu hình PATH.
)

if not exist venv (
    python -m venv venv
)

call venv\Scripts\activate.bat
python -m pip install --upgrade pip

if exist pyproject.toml (
    pip install -e ".[dev]"
) else (
    echo [Lỗi] Không tìm thấy pyproject.toml.
    exit /b 1
)

if not exist .env (
    if exist .env.example (
        copy .env.example .env
    )
)

echo === Hoàn tất thiết lập! ===
echo Kích hoạt môi trường ảo: venv\Scripts\activate.bat
echo Tùy chọn YouTube OAuth: pip install -e ".[youtube]"
pause
