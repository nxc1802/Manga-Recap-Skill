"""
src/tts/voice_profiles.py
Authoritative catalog and metadata for VieNeu-TTS v3 Turbo preset voices.
Directly aligned with official VieNeu v3 Turbo preset voices.
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
    # Top 10 Recommended Voices (Official ⭐ Editors' Picks)
    VoiceProfile("Adam bựa", "Adam bựa", AccentRegion.NORTH, VoiceGender.MALE, True, "Comic Relief / Sarcastic Narrator", "Dynamic pitch, colloquial, punchy, humorous"),
    VoiceProfile("Trúc Ly", "Trúc Ly", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Primary Narrator", "Warm, steady pacing, natural flow, clear enunciation"),
    VoiceProfile("Thiện Minh", "Thiện Minh", AccentRegion.NORTH, VoiceGender.MALE, True, "Mentor / Heroic Narrator", "Calm, measured, composed, philosophical and authoritative"),
    VoiceProfile("Mai Anh", "Mai Anh", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Heroine / News Anchor", "Gentle, melodious, innocent, bright and sweet"),
    VoiceProfile("Hải Đăng", "Hải Đăng", AccentRegion.NORTH, VoiceGender.MALE, True, "Protagonist / Male Lead", "Energetic, resolute, youthful, bright projection"),
    VoiceProfile("Thùy Dung", "Thùy Dung", AccentRegion.SOUTH, VoiceGender.FEMALE, True, "Expressive Narrator / News", "Rich vocal range, poignant emotion, high clarity"),
    VoiceProfile("Thiền Tâm Đức", "Thiền Tâm Đức", AccentRegion.NORTH, VoiceGender.MALE, True, "Veteran / Elder / Sect Leader", "Deep, grave, archaic dignity, resonant authority"),
    VoiceProfile("Ngọc Huyền", "Ngọc Huyền", AccentRegion.NORTH, VoiceGender.FEMALE, True, "Cheerful Companion / Lively Female", "Crisp, youthful, upbeat, clear enunciation"),
    VoiceProfile("Quang Sơn", "Quang Sơn", AccentRegion.CENTRAL, VoiceGender.MALE, True, "Villain / Central Storyteller", "Sharp, cold, menacing, calculated, biting edge"),
    VoiceProfile("Ngọc Trân", "Ngọc Trân", AccentRegion.CENTRAL, VoiceGender.FEMALE, True, "Noble / Central Female Lead", "Regal, proud, poised, commanding elegance"),

    # Remaining 15 Official Presets
    VoiceProfile("Minh Đức", "Minh Đức", AccentRegion.NORTH, VoiceGender.MALE, False, "Documentary / News Reporter", "Formal, articulate, deep and balanced"),
    VoiceProfile("Phạm Tuyên", "Phạm Tuyên", AccentRegion.NORTH, VoiceGender.MALE, False, "Supporting Male / Natural Flow", "Authentic, grounded, conversational"),
    VoiceProfile("Thái Sơn", "Thái Sơn", AccentRegion.SOUTH, VoiceGender.MALE, False, "Southern Lead / Dramatic Storyteller", "Resolute, energetic Southern masculine delivery"),
    VoiceProfile("Xuân Vĩnh", "Xuân Vĩnh", AccentRegion.NORTH, VoiceGender.MALE, False, "Veteran Commander / Old Guard", "Mature, experienced, calm cadence"),
    VoiceProfile("Thanh Bình", "Thanh Bình", AccentRegion.NORTH, VoiceGender.MALE, False, "Peaceful Narrator / Scholar", "Gentle, lyrical, reflective tone"),
    VoiceProfile("Ngọc Linh", "Ngọc Linh", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Classical Female / Poetic Narrator", "Soft, fluid, graceful Northern accent"),
    VoiceProfile("Đoan Trang", "Đoan Trang", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Noble Lady / Analytical Strategist", "Poised, elegant, measured pacing"),
    VoiceProfile("Thục Đoan", "Thục Đoan", AccentRegion.SOUTH, VoiceGender.FEMALE, False, "Warm Southern Narrator", "Affectionate, melodious Southern charm"),
    VoiceProfile("Minh Triết", "Minh Triết", AccentRegion.SOUTH, VoiceGender.MALE, False, "Modern Reporter / Southern Intellect", "Crisp, confident, informative tone"),
    VoiceProfile("Mỹ Duyên", "Mỹ Duyên", AccentRegion.SOUTH, VoiceGender.FEMALE, False, "Audiobook Female / Emotional Drama", "Tender, expressive, narrative depth"),
    VoiceProfile("Quỳnh Anh", "Quỳnh Anh", AccentRegion.NORTH, VoiceGender.FEMALE, False, "Audiobook Reader / Storybook Heroine", "Clear, engaging, evocative pacing"),
    VoiceProfile("Đức Trí", "Đức Trí", AccentRegion.SOUTH, VoiceGender.MALE, False, "Audiobook Reader / Supporting Companion", "Warm, mellow, soothing timber"),
    VoiceProfile("Kim Thanh", "Kim Thanh", AccentRegion.SOUTH, VoiceGender.FEMALE, False, "Lore Keeper / Experienced Matron", "Rich, dignified, emotional resonance"),
    VoiceProfile("Adam", "Adam", AccentRegion.SOUTH, VoiceGender.MALE, False, "Casual Dialogue / Modern Protagonist", "Natural, informal, easy-going projection"),
    VoiceProfile("Quốc Tuấn", "Quốc Tuấn", AccentRegion.NORTH, VoiceGender.MALE, False, "Fiery Swordsman / Martial Artist", "Bold, commanding, intense projection"),
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
