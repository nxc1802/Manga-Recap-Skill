import pytest
import os
from pathlib import Path
from PIL import Image

@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path

@pytest.fixture
def sample_image_path(temp_dir):
    img_path = temp_dir / "sample_panel.jpg"
    # Create a simple 800x600 red image
    img = Image.new("RGB", (800, 600), color="red")
    img.save(img_path)
    return str(img_path)

@pytest.fixture
def sample_portrait_image_path(temp_dir):
    img_path = temp_dir / "portrait_panel.jpg"
    # Create a 600x1200 blue portrait image
    img = Image.new("RGB", (600, 1200), color="blue")
    img.save(img_path)
    return str(img_path)

@pytest.fixture
def sample_square_image_path(temp_dir):
    img_path = temp_dir / "square_panel.jpg"
    # Create a 800x800 green square image
    img = Image.new("RGB", (800, 800), color="green")
    img.save(img_path)
    return str(img_path)

@pytest.fixture
def legacy_script_text():
    return """
    [Khung hình 1]: Trong bóng tối tĩnh mịch, Ghislain Perdium giật mình tỉnh giấc.
    [Khung hình 2]: Hắn nhìn xung quanh và tự hỏi mình đang ở đâu.
    [Khung hình 3_1]: Bỗng nhiên, một giọng nói vang lên từ phía sau.
    [Khung hình 3_2]: "Chào mừng ngươi đã đến với thế giới này."
    """

@pytest.fixture
def json_script_text():
    return """
    {
        "1": {
            "text": "Trong bóng tối tĩnh mịch, Ghislain Perdium giật mình tỉnh giấc.",
            "focus_x": 50,
            "focus_y": 50,
            "pan_direction": "horizontal",
            "crop_scale": 100
        },
        "2": {
            "text": "Hắn nhìn xung quanh và tự hỏi mình đang ở đâu."
        },
        "3_1": {
            "text": "Bỗng nhiên, một giọng nói vang lên từ phía sau."
        },
        "3_2": "Chào mừng ngươi đã đến với thế giới này."
    }
    """


@pytest.fixture(autouse=True)
def isolated_default_data_root(tmp_path, monkeypatch):
    """No deterministic test may create projects in the working repository."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "isolated_data"))
