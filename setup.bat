@echo off
:: ==============================================================================
:: Manga Recap Pipeline Orchestrator - Windows Setup Script
:: ==============================================================================
chcp 65001 >nul
echo === Khởi động Script Thiết lập Hệ thống Manga Recap ===

:: 1. Kiểm tra Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [Lỗi] Python chưa được cài đặt hoặc chưa cấu hình biến môi trường PATH.
    echo Vui lòng cài đặt Python 3.10+ trước khi chạy thiết lập này.
    exit /b 1
)

:: 2. Kiểm tra FFmpeg
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [Cảnh báo] FFmpeg chưa được cài đặt hoặc chưa cấu hình biến môi trường PATH.
    echo Bạn cần cài đặt FFmpeg và cấu hình PATH để thực hiện tạo phụ đề và dựng video.
)

:: 3. Tạo môi trường ảo venv nếu chưa tồn tại
if not exist venv (
    echo Thư mục venv chưa tồn tại. Tiến hành khởi tạo môi trường ảo...
    python -m venv venv
    echo Khởi tạo venv thành công!
) else (
    echo Môi trường ảo venv đã tồn tại.
)

:: 4. Kích hoạt môi trường ảo và cài đặt dependencies
echo Đang kích hoạt môi trường ảo và cập nhật pip...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip

if exist requirements.txt (
    echo Đang cài đặt thư viện cần thiết từ requirements.txt...
    pip install -r requirements.txt
    pip install -e .
    echo Cài đặt thư viện thành công!
) else (
    echo [Lỗi] Không tìm thấy tệp requirements.txt.
    exit /b 1
)

:: 5. Khởi tạo tệp cấu hình .env từ .env.example
if not exist .env (
    if exist .env.example (
        echo Đang tạo tệp cấu hình .env từ .env.example...
        copy .env.example .env
        echo Vui lòng mở tệp .env và cấu hình các biến môi trường nếu cần!
    ) else (
        echo Không tìm thấy .env.example. Đang tạo tệp .env trống...
        type nul > .env
    )
) else (
    echo Tệp cấu hình .env đã tồn tại.
)

echo === Hoàn tất quá trình thiết lập! ===
echo Để kích hoạt môi trường ảo chạy lệnh:
echo   venv\Scripts\activate.bat
pause
