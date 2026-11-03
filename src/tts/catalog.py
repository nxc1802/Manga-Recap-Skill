"""
src/tts/catalog.py
Comprehensive 25-voice registry for VieNeu-TTS v3 Turbo.
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
    # Top 10 Recommended Voices (Editors' Picks)
    "Trúc Ly": VoiceProfile(
        voice_id="Trúc Ly",
        name="Trúc Ly",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="narrative",
        description="Giọng nữ miền Bắc truyền cảm, ấm áp, nhịp điệu kể chuyện tự nhiên",
        recommended_for=["narration", "female_protagonist", "storytelling"],
        is_recommended=True,
    ),
    "Hải Đăng": VoiceProfile(
        voice_id="Hải Đăng",
        name="Hải Đăng",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="authoritative",
        description="Giọng nam miền Nam dõng dạc, trầm ấm, mạch lạc và nam tính",
        recommended_for=["narration", "male_protagonist", "action"],
        is_recommended=True,
    ),
    "Thiền Tâm Đức": VoiceProfile(
        voice_id="Thiền Tâm Đức",
        name="Thiền Tâm Đức",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="philosophical",
        description="Giọng nam miền Bắc trầm sâu, điềm tĩnh, phong thái cổ phong",
        recommended_for=["mentor", "elder", "lore_explanation"],
        is_recommended=True,
    ),
    "Thiện Minh": VoiceProfile(
        voice_id="Thiện Minh",
        name="Thiện Minh",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="dramatic",
        description="Giọng nam miền Bắc hào sảng, uy lực, phù hợp cao trào hành động",
        recommended_for=["antagonist", "heroic_shout", "dramatic_climax"],
        is_recommended=True,
    ),
    "Mai Anh": VoiceProfile(
        voice_id="Mai Anh",
        name="Mai Anh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="youthful",
        description="Giọng nữ miền Bắc trẻ trung, thanh thoát, trong trẻo",
        recommended_for=["young_heroine", "dialogue", "lively_scenes"],
        is_recommended=True,
    ),
    "Thùy Dung": VoiceProfile(
        voice_id="Thùy Dung",
        name="Thùy Dung",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="gentle",
        description="Giọng nữ miền Bắc dịu dàng, ngọt ngào, cảm xúc tự nhiên",
        recommended_for=["emotional_narration", "female_characters"],
        is_recommended=True,
    ),
    "Quang Sơn": VoiceProfile(
        voice_id="Quang Sơn",
        name="Quang Sơn",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="martial",
        description="Giọng nam miền Bắc sắc sảo, lạnh lùng, phong trần",
        recommended_for=["martial_arts", "antagonist"],
        is_recommended=True,
    ),
    "Ngọc Trân": VoiceProfile(
        voice_id="Ngọc Trân",
        name="Ngọc Trân",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="lyrical",
        description="Giọng nữ miền Nam nhẹ nhàng, thanh lịch, giàu cảm xúc",
        recommended_for=["poetic_moments", "supporting_female"],
        is_recommended=True,
    ),
    "Ngọc Huyền": VoiceProfile(
        voice_id="Ngọc Huyền",
        name="Ngọc Huyền",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="melodramatic",
        description="Giọng nữ miền Bắc thanh lịch, biểu cảm cao, phù hợp phân cảnh bi kịch",
        recommended_for=["drama", "sorrow", "tragic_recap"],
        is_recommended=True,
    ),
    "Adam bựa": VoiceProfile(
        voice_id="Adam bựa",
        name="Adam bựa",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="humorous",
        description="Giọng nam miền Nam hài hước, dí dỏm, phong cách châm biếm",
        recommended_for=["comedy", "parody", "satirical_narration"],
        is_recommended=True,
    ),

    # 15 Additional Regional Presets
    "Thế Khang": VoiceProfile(
        voice_id="Thế Khang",
        name="Thế Khang",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="modern",
        description="Giọng nam trẻ hiện đại, dứt khoát",
        recommended_for=["supporting_male"],
    ),
    "Bảo Châu": VoiceProfile(
        voice_id="Bảo Châu",
        name="Bảo Châu",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="playful",
        description="Giọng nữ Nam vui tươi, tinh nghịch",
        recommended_for=["playful_scenes"],
    ),
    "Minh Hoàng": VoiceProfile(
        voice_id="Minh Hoàng",
        name="Minh Hoàng",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="heroic",
        description="Giọng nam Bắc dõng dạc, uy nghiêm",
        recommended_for=["warrior"],
    ),
    "Vân Anh": VoiceProfile(
        voice_id="Vân Anh",
        name="Vân Anh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="poetic",
        description="Giọng nữ Bắc cổ điển, êm đềm",
        recommended_for=["classical_story"],
    ),
    "Hoàng Nam": VoiceProfile(
        voice_id="Hoàng Nam",
        name="Hoàng Nam",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="conversational",
        description="Giọng nam Nam gần gũi, đời thường",
        recommended_for=["casual_dialogue"],
    ),
    "Khánh Linh": VoiceProfile(
        voice_id="Khánh Linh",
        name="Khánh Linh",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="intellectual",
        description="Giọng nữ Bắc mạch lạc, thông minh",
        recommended_for=["explanation"],
    ),
    "Tuấn Kiệt": VoiceProfile(
        voice_id="Tuấn Kiệt",
        name="Tuấn Kiệt",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="sharp",
        description="Giọng nam Bắc sắc bén, quyết liệt",
        recommended_for=["rival"],
    ),
    "Phương Thảo": VoiceProfile(
        voice_id="Phương Thảo",
        name="Phương Thảo",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="maternal",
        description="Giọng nữ Bắc ấm áp, chở che",
        recommended_for=["elder_female"],
    ),
    "Đức Huy": VoiceProfile(
        voice_id="Đức Huy",
        name="Đức Huy",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="stoic",
        description="Giọng nam Bắc trầm lắng, ít nói",
        recommended_for=["silent_warrior"],
    ),
    "Hương Giang": VoiceProfile(
        voice_id="Hương Giang",
        name="Hương Giang",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.CENTRAL,
        tone="lyrical",
        description="Giọng nữ miền Trung truyền cảm, độc đáo",
        recommended_for=["central_accent"],
    ),
    "Việt Dũng": VoiceProfile(
        voice_id="Việt Dũng",
        name="Việt Dũng",
        gender=VoiceGender.MALE,
        region=VoiceRegion.NORTH,
        tone="gritty",
        description="Giọng nam Bắc dày dặn kinh nghiệm",
        recommended_for=["veteran"],
    ),
    "Thanh Trúc": VoiceProfile(
        voice_id="Thanh Trúc",
        name="Thanh Trúc",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.SOUTH,
        tone="resolute",
        description="Giọng nữ Nam kiên định, hiện đại",
        recommended_for=["action_heroine"],
    ),
    "Gia Bảo": VoiceProfile(
        voice_id="Gia Bảo",
        name="Gia Bảo",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="youthful",
        description="Giọng nam Nam trẻ trung, nhanh nhẹn",
        recommended_for=["young_boy"],
    ),
    "Thu Hà": VoiceProfile(
        voice_id="Thu Hà",
        name="Thu Hà",
        gender=VoiceGender.FEMALE,
        region=VoiceRegion.NORTH,
        tone="solemn",
        description="Giọng nữ Bắc nghiêm trang, chỉn chu",
        recommended_for=["history_narration"],
    ),
    "Trọng Hiếu": VoiceProfile(
        voice_id="Trọng Hiếu",
        name="Trọng Hiếu",
        gender=VoiceGender.MALE,
        region=VoiceRegion.SOUTH,
        tone="charismatic",
        description="Giọng nam Nam phóng khoáng, lãng tử",
        recommended_for=["rogue"],
    ),
}

VOICE_ALIASES: Dict[str, str] = {
    "default": "Trúc Ly",
    "female_narrator": "Trúc Ly",
    "male_narrator": "Hải Đăng",
    "truc_ly": "Trúc Ly",
    "trucly": "Trúc Ly",
    "hai_dang": "Hải Đăng",
    "haidang": "Hải Đăng",
    "thien_tam_duc": "Thiền Tâm Đức",
    "thien_minh": "Thiện Minh",
    "mai_anh": "Mai Anh",
    "thuy_dung": "Thùy Dung",
    "quang_son": "Quang Sơn",
    "ngoc_tran": "Ngọc Trân",
    "ngoc_huyen": "Ngọc Huyền",
    "adam_bua": "Adam bựa",
    "the_khang": "Thế Khang",
    "bao_chau": "Bảo Châu",
    "minh_hoang": "Minh Hoàng",
    "van_anh": "Vân Anh",
    "hoang_nam": "Hoàng Nam",
    "khanh_linh": "Khánh Linh",
    "tuan_kiet": "Tuấn Kiệt",
    "phuong_thao": "Phương Thảo",
    "duc_huy": "Đức Huy",
    "huong_giang": "Hương Giang",
    "viet_dung": "Việt Dũng",
    "thanh_truc": "Thanh Trúc",
    "gia_bao": "Gia Bảo",
    "thu_ha": "Thu Hà",
    "trong_hieu": "Trọng Hiếu",
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
