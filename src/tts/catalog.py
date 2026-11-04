"""
src/tts/catalog.py
Comprehensive 25-voice registry for VieNeu-TTS v3 Turbo.
Directly aligned with official VieNeu v3 Turbo preset voices.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class VoiceRegion(str, Enum):
    NORTH = "north"
    CENTRAL = "central"
    SOUTH = "south"


class VoiceGender(str, Enum):
    FEMALE = "female"
    MALE = "male"


class VoiceProfile(BaseModel):
    voice_id: str
    name: str
    gender: VoiceGender
    region: VoiceRegion
    tone: str
    description: str
    recommended_for: List[str] = Field(default_factory=list)
    is_recommended: bool = False
    default_speed: float = 1.0


CATALOG_VOICES: Dict[str, VoiceProfile] = {
    # Top 10 Recommended Voices (Official ⭐ Editors' Picks)
    "Adam bựa": VoiceProfile(
        voice_id="Adam bựa",
        name="Adam bựa",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="humorous",
        description="⭐ Adam bựa — Nam · Bắc · Phong cách tự nhiên, dí dỏm, hài hước",
        recommended_for=["comedy", "parody", "satirical_narration"],
        is_recommended=True,
    ),
    "Trúc Ly": VoiceProfile(
        voice_id="Trúc Ly",
        name="Trúc Ly",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="narrative",
        description="⭐ Trúc Ly — Nữ · Bắc · Phong cách tự nhiên, truyền cảm, ấm áp",
        recommended_for=["narration", "female_protagonist", "storytelling"],
        is_recommended=True,
    ),
    "Thiện Minh": VoiceProfile(
        voice_id="Thiện Minh",
        name="Thiện Minh",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="storytelling",
        description="⭐ Thiện Minh — Nam · Bắc · Phong cách kể chuyện, hào sảng, uy lực",
        recommended_for=["narration", "male_protagonist", "heroic_shout", "dramatic_climax"],
        is_recommended=True,
    ),
    "Mai Anh": VoiceProfile(
        voice_id="Mai Anh",
        name="Mai Anh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="news",
        description="⭐ Mai Anh — Nữ · Bắc · Phong cách tin tức, thanh thoát, trong trẻo",
        recommended_for=["young_heroine", "dialogue", "lively_scenes", "news"],
        is_recommended=True,
    ),
    "Hải Đăng": VoiceProfile(
        voice_id="Hải Đăng",
        name="Hải Đăng",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="⭐ Hải Đăng — Nam · Bắc · Phong cách tự nhiên, dõng dạc, mạch lạc",
        recommended_for=["narration", "male_protagonist", "action"],
        is_recommended=True,
    ),
    "Thùy Dung": VoiceProfile(
        voice_id="Thùy Dung",
        name="Thùy Dung",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="news",
        description="⭐ Thùy Dung — Nữ · Nam · Phong cách tin tức, dịu dàng, ngọt ngào",
        recommended_for=["emotional_narration", "female_characters", "southern_accent"],
        is_recommended=True,
    ),
    "Thiền Tâm Đức": VoiceProfile(
        voice_id="Thiền Tâm Đức",
        name="Thiền Tâm Đức",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="philosophical",
        description="⭐ Thiền Tâm Đức — Nam · Bắc · Phong cách kể chuyện, trầm sâu, điềm tĩnh",
        recommended_for=["mentor", "elder", "lore_explanation", "sect_leader"],
        is_recommended=True,
    ),
    "Ngọc Huyền": VoiceProfile(
        voice_id="Ngọc Huyền",
        name="Ngọc Huyền",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="⭐ Ngọc Huyền — Nữ · Bắc · Giọng đọc tự nhiên, thanh lịch, biểu cảm cao",
        recommended_for=["drama", "sorrow", "tragic_recap", "supporting_female"],
        is_recommended=True,
    ),
    "Quang Sơn": VoiceProfile(
        voice_id="Quang Sơn",
        name="Quang Sơn",
        gender=VoiceGender.MALE,
        region=VoiceRegion.CENTRAL,
        tone="natural",
        description="⭐ Quang Sơn — Nam · Trung · Phong cách tự nhiên, sắc sảo, lạnh lùng",
        recommended_for=["martial_arts", "antagonist", "central_accent"],
        is_recommended=True,
    ),
    "Ngọc Trân": VoiceProfile(
        voice_id="Ngọc Trân",
        name="Ngọc Trân",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.CENTRAL,
        tone="natural",
        description="⭐ Ngọc Trân — Nữ · Trung · Phong cách tự nhiên, nhẹ nhàng, thanh lịch",
        recommended_for=["poetic_moments", "supporting_female", "central_accent"],
        is_recommended=True,
    ),

    # 15 Additional Official Regional Presets
    "Minh Đức": VoiceProfile(
        voice_id="Minh Đức",
        name="Minh Đức",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="news",
        description="Minh Đức — Nam · Bắc · Phong cách tin tức, đĩnh đạc",
        recommended_for=["news", "narration", "documentary"],
    ),
    "Phạm Tuyên": VoiceProfile(
        voice_id="Phạm Tuyên",
        name="Phạm Tuyên",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="Phạm Tuyên — Nam · Bắc · Phong cách tự nhiên, chân thực",
        recommended_for=["supporting_male", "casual_narration"],
    ),
    "Thái Sơn": VoiceProfile(
        voice_id="Thái Sơn",
        name="Thái Sơn",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="storytelling",
        description="Thái Sơn — Nam · Nam · Phong cách kể chuyện hào sảng",
        recommended_for=["southern_narration", "male_protagonist"],
    ),
    "Xuân Vĩnh": VoiceProfile(
        voice_id="Xuân Vĩnh",
        name="Xuân Vĩnh",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="Xuân Vĩnh — Nam · Bắc · Phong cách tự nhiên, chững chạc",
        recommended_for=["veteran", "elder"],
    ),
    "Thanh Bình": VoiceProfile(
        voice_id="Thanh Bình",
        name="Thanh Bình",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="storytelling",
        description="Thanh Bình — Nam · Bắc · Phong cách kể chuyện nhẹ nhàng, sâu lắng",
        recommended_for=["storytelling", "peaceful_scenes"],
    ),
    "Ngọc Linh": VoiceProfile(
        voice_id="Ngọc Linh",
        name="Ngọc Linh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="storytelling",
        description="Ngọc Linh — Nữ · Bắc · Phong cách kể chuyện ngọt ngào, cổ điển",
        recommended_for=["classical_story", "female_narration"],
    ),
    "Đoan Trang": VoiceProfile(
        voice_id="Đoan Trang",
        name="Đoan Trang",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="Đoan Trang — Nữ · Bắc · Phong cách tự nhiên, thanh tao",
        recommended_for=["noble_female", "calm_narration"],
    ),
    "Thục Đoan": VoiceProfile(
        voice_id="Thục Đoan",
        name="Thục Đoan",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="storytelling",
        description="Thục Đoan — Nữ · Nam · Phong cách kể chuyện miền Nam truyền cảm",
        recommended_for=["southern_female", "storytelling"],
    ),
    "Minh Triết": VoiceProfile(
        voice_id="Minh Triết",
        name="Minh Triết",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="news",
        description="Minh Triết — Nam · Nam · Phong cách tin tức dõng dạc",
        recommended_for=["news", "explanation"],
    ),
    "Mỹ Duyên": VoiceProfile(
        voice_id="Mỹ Duyên",
        name="Mỹ Duyên",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="reading",
        description="Mỹ Duyên — Nữ · Nam · Phong cách đọc truyện giàu cảm xúc",
        recommended_for=["audiobook", "emotional_scenes"],
    ),
    "Quỳnh Anh": VoiceProfile(
        voice_id="Quỳnh Anh",
        name="Quỳnh Anh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="reading",
        description="Quỳnh Anh — Nữ · Bắc · Phong cách đọc truyện mượt mà",
        recommended_for=["audiobook", "female_narration"],
    ),
    "Đức Trí": VoiceProfile(
        voice_id="Đức Trí",
        name="Đức Trí",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="reading",
        description="Đức Trí — Nam · Nam · Phong cách đọc truyện trầm ấm",
        recommended_for=["audiobook", "supporting_male"],
    ),
    "Kim Thanh": VoiceProfile(
        voice_id="Kim Thanh",
        name="Kim Thanh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="reading",
        description="Kim Thanh — Nữ · Nam · Phong cách đọc truyện thanh lịch",
        recommended_for=["audiobook", "classical_female"],
    ),
    "Adam": VoiceProfile(
        voice_id="Adam",
        name="Adam",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="natural",
        description="Adam — Nam · Nam · Giọng đọc tự nhiên, phóng khoáng",
        recommended_for=["casual_dialogue", "modern_narration"],
    ),
    "Quốc Tuấn": VoiceProfile(
        voice_id="Quốc Tuấn",
        name="Quốc Tuấn",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="natural",
        description="Quốc Tuấn — Nam · Bắc · Phong cách tự nhiên, nam tính, dứt khoát",
        recommended_for=["action", "warrior", "heroic"],
    ),
}

VOICE_ALIASES: Dict[str, str] = {
    # Primary aliases
    "default": "Trúc Ly",
    "female_narrator": "Trúc Ly",
    "male_narrator": "Hải Đăng",

    # Official voices slug mapping
    "adam_bua": "Adam bựa",
    "adambua": "Adam bựa",
    "truc_ly": "Trúc Ly",
    "trucly": "Trúc Ly",
    "thien_minh": "Thiện Minh",
    "thienminh": "Thiện Minh",
    "mai_anh": "Mai Anh",
    "maianh": "Mai Anh",
    "hai_dang": "Hải Đăng",
    "haidang": "Hải Đăng",
    "thuy_dung": "Thùy Dung",
    "thuydung": "Thùy Dung",
    "thien_tam_duc": "Thiền Tâm Đức",
    "thientamduc": "Thiền Tâm Đức",
    "ngoc_huyen": "Ngọc Huyền",
    "ngochuyen": "Ngọc Huyền",
    "quang_son": "Quang Sơn",
    "quangson": "Quang Sơn",
    "ngoc_tran": "Ngọc Trân",
    "ngoctran": "Ngọc Trân",
    "minh_duc": "Minh Đức",
    "minhduc": "Minh Đức",
    "pham_tuyen": "Phạm Tuyên",
    "phamtuyen": "Phạm Tuyên",
    "thai_son": "Thái Sơn",
    "thaison": "Thái Sơn",
    "xuan_vinh": "Xuân Vĩnh",
    "xuanvinh": "Xuân Vĩnh",
    "thanh_binh": "Thanh Bình",
    "thanhbinh": "Thanh Bình",
    "ngoc_linh": "Ngọc Linh",
    "ngoclinh": "Ngọc Linh",
    "doan_trang": "Đoan Trang",
    "doantrang": "Đoan Trang",
    "thuc_doan": "Thục Đoan",
    "thucdoan": "Thục Đoan",
    "minh_triet": "Minh Triết",
    "minhtriet": "Minh Triết",
    "my_duyen": "Mỹ Duyên",
    "myduyen": "Mỹ Duyên",
    "quynh_anh": "Quỳnh Anh",
    "quynhanh": "Quỳnh Anh",
    "duc_tri": "Đức Trí",
    "ductri": "Đức Trí",
    "kim_thanh": "Kim Thanh",
    "kimthanh": "Kim Thanh",
    "adam": "Adam",
    "quoc_tuan": "Quốc Tuấn",
    "quoctuan": "Quốc Tuấn",

    # Compatibility mappings for removed unofficial names to nearest official counterpart
    "the_khang": "Quốc Tuấn",
    "bao_chau": "Ngọc Huyền",
    "minh_hoang": "Thiện Minh",
    "van_anh": "Ngọc Linh",
    "hoang_nam": "Adam",
    "khanh_linh": "Đoan Trang",
    "tuan_kiet": "Quốc Tuấn",
    "phuong_thao": "Quỳnh Anh",
    "duc_huy": "Phạm Tuyên",
    "huong_giang": "Ngọc Trân",
    "viet_dung": "Minh Đức",
    "thanh_truc": "Thục Đoan",
    "gia_bao": "Thái Sơn",
    "thu_ha": "Mai Anh",
    "trong_hieu": "Đức Trí",

    # Legacy Cloud Redirection
    "edge-tts-female": "Trúc Ly",
    "edge-tts-male": "Hải Đăng",
    "edge-tts-en-female": "Trúc Ly",
    "edge-tts-en-male": "Hải Đăng",
    "gemini-2.5-flash": "Trúc Ly",
    "gemini-3.1-flash": "Trúc Ly",
    "openai-tts-nova": "Trúc Ly",
    "openai-tts-alloy": "Hải Đăng",
}


def is_valid_voice_id(name: Optional[str]) -> bool:
    """Checks whether the given voice name directly matches an official catalog voice or recognized alias."""
    if not name or not isinstance(name, str):
        return False
    cleaned = name.strip()
    if cleaned in CATALOG_VOICES:
        return True
    lower_norm = cleaned.lower().replace("-", "_").replace(" ", "_")
    if lower_norm in VOICE_ALIASES or cleaned in VOICE_ALIASES:
        return True
    for vid in CATALOG_VOICES:
        if vid.lower() == cleaned.lower():
            return True
    return False


def resolve_voice_id(name: Optional[str]) -> str:
    """Resolves arbitrary voice string, alias, or legacy name to valid VieNeu voice_id."""
    if not name:
        return "Trúc Ly"
    cleaned = name.strip()
    if cleaned in CATALOG_VOICES:
        return cleaned

    lower_norm = cleaned.lower().replace("-", "_").replace(" ", "_")
    if lower_norm in VOICE_ALIASES:
        return VOICE_ALIASES[lower_norm]
    if cleaned in VOICE_ALIASES:
        return VOICE_ALIASES[cleaned]

    # Fuzzy match on name without accents or case
    for vid in CATALOG_VOICES:
        if vid.lower() == cleaned.lower():
            return vid

    return "Trúc Ly"


def get_voice_profile(voice_id: str) -> VoiceProfile:
    """Returns VoiceProfile for given voice_id or alias."""
    resolved = resolve_voice_id(voice_id)
    return CATALOG_VOICES[resolved]
