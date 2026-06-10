#!/bin/bash

# ==============================================================================
# Manga Recap Pipeline Orchestrator - Unix Setup Script
# Works on macOS and Linux
# ==============================================================================

# Exit immediately if a command exits with a non-zero status
set -e

# ANSI Color Codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Khởi động Script Thiết lập Hệ thống Manga Recap ===${NC}"

# 1. Kiểm tra Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Lỗi: python3 chưa được cài đặt. Vui lòng cài đặt Python 3.10+ trước.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "Phiên bản Python hiện tại: ${GREEN}$PYTHON_VERSION${NC}"

# 2. Kiểm tra FFmpeg (quan trọng cho video rendering)
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${YELLOW}Cảnh báo: ffmpeg chưa được cài đặt hoặc chưa cấu hình biến môi trường PATH.${NC}"
    echo -e "Hệ thống cần FFmpeg để ghép phụ đề và xuất video thành phẩm."
    echo -e "Hướng dẫn cài đặt nhanh:"
    echo -e "  - macOS: brew install ffmpeg"
    echo -e "  - Ubuntu/Debian: sudo apt install ffmpeg"
else
    echo -e "Đã cài đặt FFmpeg: ${GREEN}$(ffmpeg -version | head -n 1)${NC}"
fi

# 3. Tạo môi trường ảo venv nếu chưa tồn tại
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Thư mục venv chưa tồn tại. Tiến hành khởi tạo môi trường ảo Python...${NC}"
    python3 -m venv venv
    echo -e "${GREEN}Khởi tạo venv thành công!${NC}"
else
    echo -e "Môi trường ảo ${GREEN}venv${NC} đã tồn tại."
fi

# 4. Kích hoạt môi trường ảo và cài đặt dependencies
echo -e "Đang kích hoạt môi trường ảo và cập nhật pip..."
source venv/bin/activate
pip install --upgrade pip

if [ -f "requirements.txt" ]; then
    echo -e "Đang cài đặt thư viện cần thiết từ ${GREEN}requirements.txt${NC}..."
    pip install -r requirements.txt
    echo -e "${GREEN}Cài đặt thư viện thành công!${NC}"
else
    echo -e "${RED}Lỗi: Không tìm thấy tệp requirements.txt.${NC}"
    exit 1
fi

# 5. Khởi tạo tệp cấu hình .env từ .env.example
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo -e "Đang tạo tệp cấu hình ${GREEN}.env${NC} từ ${GREEN}.env.example${NC}..."
        cp .env.example .env
        echo -e "${YELLOW}Vui lòng mở tệp .env và cập nhật các khóa API (OpenRouter/Supabase) của bạn!${NC}"
    else
        echo -e "Không tìm thấy .env.example. Đang tạo tệp .env trống..."
        echo -e "OPENROUTER_API_KEY=\nSUPABASE_URL=\nSUPABASE_ANON_KEY=\nSUPABASE_SERVICE_ROLE_KEY=" > .env
    fi
else
    echo -e "Tệp cấu hình ${GREEN}.env${NC} đã tồn tại."
fi

echo -e "${GREEN}=== Hoàn tất quá trình thiết lập! ===${NC}"
echo -e "Để kích hoạt môi trường ảo chạy lệnh:"
echo -e "  ${YELLOW}source venv/bin/activate${NC}"
echo -e "Để chạy backend hoặc UI, vui lòng tham khảo ${GREEN}README.md${NC}."
