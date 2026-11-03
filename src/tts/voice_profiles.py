"""
src/tts/voice_profiles.py
Authoritative catalog and metadata for VieNeu-TTS v3 Turbo preset voices.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from src.tts.catalog import CATALOG_VOICES, VoiceGender as BaseGender, VoiceProfile as BaseProfile, VoiceRegion as BaseRegion


class AccentRegion(str, Enum):
    NORTH = "Bắc"
    SOUTH = "Nam"
    CENTRAL = "Trung"


class VoiceGender(str, Enum):
    FEMALE = "female"
    MALE = "male"


@dataclass(frozen=True)
class VoiceProfile:
    voice_id: str
    name: str
    region: AccentRegion
    gender: VoiceGender
    is_top10: bool
    suggested_role: str
    timbre_description: str
    default_speed: float = 1.0


# Authoritative 25-Voice Catalog
VOICE_CATALOG_25: List[VoiceProfile] = [
    # Top 10 Recommended Voices
    VoiceProfile("Trúc Ly", "Trúc Ly", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Primary Narrator", "Warm, steady pacing, natural flow, clear enunciation"),
    VoiceProfile("Hải Đăng", "Hải Đăng", AccentRegion.SOUTH, VoiceGender.MALE, True, "Protagonist / Male Lead", "Energetic, resolute, youthful, bright projection"),
    VoiceProfile("Thiền Tâm Đức", "Thiền Tâm Đức", AccentRegion.NORTH, VoiceGender.MALE, True, "Veteran / Elder / Sect Leader", "Deep, grave, archaic dignity, resonant authority"),
    VoiceProfile("Thiện Minh", "Thiện Minh", AccentRegion.NORTH, VoiceGender.MALE, True, "Mentor / Strategist / Scholar", "Calm, measured, composed, philosophical"),
    VoiceProfile("Quang Sơn", "Quang Sơn", AccentRegion.NORTH, VoiceGender.MALE, True, "Villain / Rival / Antagonist", "Sharp, cold, menacing, calculated, biting edge"),
    VoiceProfile("Mai Anh", "Mai Anh", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Heroine / Female Lead", "Gentle, melodious, innocent, bright and sweet"),
    VoiceProfile("Ngọc Trân", "Ngọc Trân", AccentRegion.SOUTH, VoiceGender.FEMALE, True, "Noble / Empress / Senior Cultivator", "Regal, proud, poised, commanding elegance"),
    VoiceProfile("Adam bựa", "Adam bựa", AccentRegion.SOUTH, VoiceGender.MALE, True, "Comic Relief / Sarcastic Narrator", "Dynamic pitch, colloquial, punchy, humorous"),
    VoiceProfile("Thùy Dung", "Thùy Dung", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Expressive Narrator / Dramatic Lead", "Rich vocal range, poignant emotion, high clarity"),
    VoiceProfile("Ngọc Huyền", "Ngọc Huyền", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Cheerful Companion / Lively Female", "Crisp, youthful, upbeat, clear enunciation"),

    # Remaining 15 Presets
    VoiceProfile("Thế Khang", "Thế Khang", AccentRegion.SOUTH, VoiceGender.MALE, False, "Supporting Male / Loyal Companion", "Direct, spirited, frank, grounded"),
    VoiceProfile("Bảo Châu", "Bảo Châu", AccentRegion.SOUTH, VoiceGender.FEMALE, False, "Playful Girl / Lively Companion", "Vivacious, sweet, fast rhythmic delivery"),
    VoiceProfile("Minh Hoàng", "Minh Hoàng", AccentRegion.NORTH, VoiceGender.MALE, False, "Heroic General / Decisive Warrior", "Bold, authoritative projection, martial tone"),
    VoiceProfile("Vân Anh", "Vân Anh", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Classical Female / Poetic Narrator", "Soft, fluid, melancholic undertone, smooth"),
    VoiceProfile("Hoàng Nam", "Hoàng Nam", AccentRegion.SOUTH, VoiceGender.MALE, False, "Modern Narrator / Casual Recapper", "Approachable, smooth, conversational pace"),
    VoiceProfile("Khánh Linh", "Khánh Linh", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Intellectual Mage / Female Strategist", "Articulate, precise, cool and analytical"),
    VoiceProfile("Tuấn Kiệt", "Tuấn Kiệt", AccentRegion.NORTH, VoiceGender.MALE, False, "Fiery Rival / Impulsive Swordsman", "Rapid delivery, sharp consonants, intense"),
    VoiceProfile("Phương Thảo", "Phương Thảo", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Maternal Elder / Healer", "Soothing, empathetic, slow measured cadence"),
    VoiceProfile("Đức Huy", "Đức Huy", AccentRegion.NORTH, VoiceGender.MALE, False, "Shadow Assassin / Stoic Guard", "Low pitch, curt, monotone, stealthy composure"),
    VoiceProfile("Hương Giang", "Hương Giang", AccentRegion.CENTRAL, VoiceGender.FEMALE, False, "Mystic Seer / Central Storyteller", "Unique Central accent, lyrical, atmospheric"),
    VoiceProfile("Việt Dũng", "Việt Dũng", AccentRegion.NORTH, VoiceGender.MALE, False, "Veteran Commander / Old Guard", "Gritty, experienced, commanding presence"),
    VoiceProfile("Thanh Trúc", "Thanh Trúc", AccentRegion.SOUTH, VoiceGender.FEMALE, False, "Adventurer / Independent Heroine", "Resolute, clear, modern Southern accent"),
    VoiceProfile("Gia Bảo", "Gia Bảo", AccentRegion.SOUTH, VoiceGender.MALE, False, "Young Disciple / Inquisitive Boy", "High pitch, energetic, naive, quick-paced"),
    VoiceProfile("Thu Hà", "Thu Hà", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Lore Keeper / Serious Narrator", "Solemn, formal, steady, dramatic depth"),
    VoiceProfile("Trọng Hiếu", "Trọng Hiếu", AccentRegion.SOUTH, VoiceGender.MALE, False, "Wandering Rogue / Charming Sword", "Relaxed, confident, charismatic drawl"),
]

VOICE_BY_NAME: Dict[str, VoiceProfile] = {vp.name: vp for vp in VOICE_CATALOG_25}


def get_voice_profile(name_or_id: str) -> Optional[VoiceProfile]:
    return VOICE_BY_NAME.get(name_or_id)


def list_top10_voices() -> List[VoiceProfile]:
    return [vp for vp in VOICE_CATALOG_25 if vp.is_top10]


def filter_voices(region: Optional[AccentRegion] = None, gender: Optional[VoiceGender] = None) -> List[VoiceProfile]:
    results = VOICE_CATALOG_25
    if region:
        results = [vp for vp in results if vp.region == region]
    if gender:
        results = [vp for vp in results if vp.gender == gender]
    return results
