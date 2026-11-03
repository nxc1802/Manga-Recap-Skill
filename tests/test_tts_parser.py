import pytest
from src.services.tts_service import TTSService

def test_parse_legacy_text_format(legacy_script_text):
    service = TTSService()
    panels = service.parse_script_text(legacy_script_text)
    
    assert len(panels) == 4
    
    # Check first panel
    assert panels[0]["sequence_id"] == "1"
    assert panels[0]["sequence_number"] == 1
    assert "Trong bóng tối tĩnh mịch" in panels[0]["text"]
    
    # Check panel 3_1 and 3_2 sorting
    assert panels[2]["sequence_id"] == "3_1"
    assert panels[2]["sequence_number"] == 3
    assert "Bỗng nhiên" in panels[2]["text"]
    
    assert panels[3]["sequence_id"] == "3_2"
    assert "Chào mừng" in panels[3]["text"]

def test_parse_json_format(json_script_text):
    service = TTSService()
    panels = service.parse_script_text(json_script_text)
    
    assert len(panels) == 4
    
    # Check first panel
    assert panels[0]["sequence_id"] == "1"
    assert panels[0]["sequence_number"] == 1
    assert "Trong bóng tối" in panels[0]["text"]
    
    # Check last panel parsed from shorthand string
    assert panels[3]["sequence_id"] == "3_2"
    assert "Chào mừng" in panels[3]["text"]

def test_parse_empty_text():
    service = TTSService()
    panels = service.parse_script_text("")
    assert panels == []
    
    panels = service.parse_script_text("random text without markers")
    assert panels == []
