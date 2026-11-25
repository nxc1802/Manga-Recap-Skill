#!/bin/bash

# ==============================================================================
# Manga Recap Pipeline Orchestrator - Unix Setup Script
# Works on macOS and Linux (installs from pyproject.toml)
# ==============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Khởi động Script Thiết lập Hệ thống Manga Recap ===${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Lỗi: python3 chưa được cài đặt. Vui lòng cài đặt Python 3.10+ trước.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "Phiên bản Python hiện tại: ${GREEN}$PYTHON_VERSION${NC}"

if ! command -v ffmpeg &> /dev/null; then
    echo -e "${YELLOW}Cảnh báo: ffmpeg chưa được cài đặt hoặc chưa cấu hình biến môi trường PATH.${NC}"
    echo -e "  - macOS: brew install ffmpeg"
    echo -e "  - Ubuntu/Debian: sudo apt install ffmpeg"
else
    echo -e "Đã cài đặt FFmpeg: ${GREEN}$(ffmpeg -version | head -n 1)${NC}"
fi

if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Tiến hành khởi tạo môi trường ảo Python (venv)...${NC}"
    python3 -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip

if [ -f "pyproject.toml" ]; then
    echo -e "Đang cài đặt package và dependencies từ ${GREEN}pyproject.toml${NC}..."
    pip install -e ".[dev]"
    echo -e "${GREEN}Cài đặt thành công!${NC}"
else
    echo -e "${RED}Lỗi: Không tìm thấy pyproject.toml.${NC}"
    exit 1
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo -e "${YELLOW}Đã tạo .env từ .env.example. Vui lòng cấu hình biến môi trường nếu cần!${NC}"
fi

echo -e "${GREEN}=== Hoàn tất thiết lập! ===${NC}"
echo -e "Kích hoạt môi trường ảo: ${YELLOW}source venv/bin/activate${NC}"
echo -e "Tùy chọn YouTube OAuth: ${YELLOW}pip install -e '.[youtube]'${NC}"
