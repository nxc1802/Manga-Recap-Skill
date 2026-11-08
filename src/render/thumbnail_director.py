"""
src/render/thumbnail_director.py
PR-06: Thumbnail Director — Key-Frame Selection & Source-Grounded 16:9 Composition.
100% local offline Pillow rendering with safe-zone exclusion and typography overlays.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src.core.logger import logger
from src.core.schemas import (
    CompositionFeatures,
    TextSafeZone,
    ThumbnailCandidate,
    ThumbnailCandidatesDocument,
    utc_iso_now,
)
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    read_json,
)


class ThumbnailComposer:
    """Renders 16:9 thumbnail variants using Pillow with blurred wings and high contrast."""

    TARGET_WIDTH = 1280
    TARGET_HEIGHT = 720

    @classmethod
    def compose_thumbnail(
        cls,
        source_image_path: Path,
        output_clean_path: Path,
        output_text_path: Optional[Path] = None,
        focus_box: Tuple[float, float, float, float] = (0.1, 0.1, 0.9, 0.9),
        overlay_text: Optional[str] = None,
        text_safe_zone: TextSafeZone = TextSafeZone.TOP_LEFT,
    ) -> Tuple[Path, Optional[Path]]:
        """
        Composes a 16:9 thumbnail from a panel image:
        - Centers on focus_box
        - Applies blurred wing background if aspect ratio is narrower than 16:9
        - Boosts contrast (+15%) and saturation (+20%)
        - Generates clean variant and optional typography mockup variant
        """
        output_clean_path.parent.mkdir(parents=True, exist_ok=True)
        if output_text_path:
            output_text_path.parent.mkdir(parents=True, exist_ok=True)

        with Image.open(source_image_path) as orig_img:
            img = orig_img.convert("RGB")
            img_w, img_h = img.size
            aspect = img_w / float(img_h)

            canvas = Image.new("RGB", (cls.TARGET_WIDTH, cls.TARGET_HEIGHT), (20, 20, 20))

            target_aspect = cls.TARGET_WIDTH / float(cls.TARGET_HEIGHT)  # ~1.777

            if aspect >= target_aspect * 0.9 and aspect <= target_aspect * 1.1:
                # Close to 16:9 — crop directly around focus center
                fx1, fy1, fx2, fy2 = focus_box
                cx = int(((fx1 + fx2) / 2.0) * img_w)
                cy = int(((fy1 + fy2) / 2.0) * img_h)

                crop_w = img_w
                crop_h = int(img_w / target_aspect)
                if crop_h > img_h:
                    crop_h = img_h
                    crop_w = int(img_h * target_aspect)

                left = max(0, min(cx - crop_w // 2, img_w - crop_w))
                top = max(0, min(cy - crop_h // 2, img_h - crop_h))
                cropped = img.crop((left, top, left + crop_w, top + crop_h))
                canvas = cropped.resize((cls.TARGET_WIDTH, cls.TARGET_HEIGHT), Image.Resampling.LANCZOS)
            else:
                # Vertical / portrait panel — use cinematic blurred wings
                # 1. Background: stretch and blur
                bg = img.resize((cls.TARGET_WIDTH, cls.TARGET_HEIGHT), Image.Resampling.BILINEAR)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=25))
                canvas.paste(bg, (0, 0))

                # 2. Foreground: scale to fit height 720
                scale = cls.TARGET_HEIGHT / float(img_h)
                fg_w = int(img_w * scale)
                fg_h = cls.TARGET_HEIGHT

                # If fg_w > target_width, clamp
                if fg_w > cls.TARGET_WIDTH:
                    scale = cls.TARGET_WIDTH / float(img_w)
                    fg_w = cls.TARGET_WIDTH
                    fg_h = int(img_h * scale)

                fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
                x_pos = (cls.TARGET_WIDTH - fg_w) // 2
                y_pos = (cls.TARGET_HEIGHT - fg_h) // 2
                canvas.paste(fg, (x_pos, y_pos))

            # 3. Enhance vibrancy and contrast for mobile CTR pop
            enhancer_contrast = ImageEnhance.Contrast(canvas)
            canvas = enhancer_contrast.enhance(1.15)
            enhancer_color = ImageEnhance.Color(canvas)
            canvas = enhancer_color.enhance(1.20)

            # Save clean variant
            canvas.save(output_clean_path, format="JPEG", quality=95)

            # 4. Optional Text Overlay Mockup Variant
            if output_text_path and overlay_text:
                text_canvas = canvas.copy()
                draw = ImageDraw.Draw(text_canvas)

                font = cls._load_font(size=64)
                words = overlay_text.strip().split()
                if len(words) > 5:
                    clean_text = " ".join(words[:5]) + "!"
                else:
                    clean_text = overlay_text.strip().upper()

                # Determine safe text placement (avoiding bottom-right 25%x20%)
                if text_safe_zone == TextSafeZone.TOP_CENTER:
                    bbox = draw.textbbox((0, 0), clean_text, font=font)
                    tw = bbox[2] - bbox[0]
                    tx = (cls.TARGET_WIDTH - tw) // 2
                    ty = 40
                elif text_safe_zone == TextSafeZone.BOTTOM_LEFT:
                    tx = 50
                    ty = cls.TARGET_HEIGHT - 120
                else:  # TOP_LEFT default
                    tx = 50
                    ty = 40

                # Draw thick stroke (black outline) + vibrant yellow/white text
                draw.text(
                    (tx, ty),
                    clean_text,
                    font=font,
                    fill="#FFE500",  # YouTube highlight yellow
                    stroke_width=6,
                    stroke_fill="#000000",
                )
                text_canvas.save(output_text_path, format="JPEG", quality=95)

        return output_clean_path, output_text_path

    @classmethod
    def _load_font(cls, size: int = 64) -> ImageFont.ImageFont:
        """Loads a TrueType font with cross-platform fallback."""
        candidate_paths = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
            "/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf",
            "/System/Library/Fonts/Supplemental/Impact.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        for font_path in candidate_paths:
            if os.path.exists(font_path):
                try:
                    return ImageFont.truetype(font_path, size=size)
                except Exception:
                    pass
        return ImageFont.load_default()


class ThumbnailCandidateDirector:
    """Evaluates, ranks, and produces key-frame thumbnail candidates for a chapter or feature."""

    EMOTION_KEYWORDS = {
        "climax": ["thức tỉnh", "biến đổi", "sát khí", "đột phá", "đại chiến", "tuyệt kỹ", "bộc phát"],
        "shock": ["kinh hãi", "không thể nào", "bất ngờ", "sững sờ", "sốc", "chấn động"],
        "action": ["giao đấu", "quyết đấu", "tung đòn", "va chạm", "trảm", "đánh"],
        "confrontation": ["đối đầu", "ngươi dám", "ai cho phép", "khiêu khích"],
    }

    def evaluate_chapter(
        self,
        project_slug: str,
        chapter_number: str,
    ) -> ThumbnailCandidatesDocument:
        """
        Scans chapter manifest, script, and panels to evaluate and compose Top 3 thumbnail candidates.
        """
        proj_dir = get_project_dir(project_slug)
        chap_dir = get_chapter_dir(project_slug, chapter_number)

        manifest_path = chap_dir / "manifest.json"
        script_path = chap_dir / "script.json"

        panels_dir = chap_dir / "panels"
        if not panels_dir.exists():
            panels_dir = chap_dir / "raw"

        manifest_data = read_json(manifest_path) if manifest_path.exists() else {}
        script_data = read_json(script_path) if script_path.exists() else {}

        panels = manifest_data.get("panels", [])
        segments = script_data.get("segments", [])

        # Map segment info by panel_id
        panel_seg_map: Dict[str, Dict[str, Any]] = {}
        for s_idx, seg in enumerate(segments):
            p_ids = seg.get("panel_ids", [])
            for pid in p_ids:
                if pid not in panel_seg_map:
                    panel_seg_map[pid] = {
                        "segment_index": s_idx,
                        "narrator_text": seg.get("narrator_text", ""),
                        "character": seg.get("character", ""),
                        "focus_box": seg.get("focus_box"),
                        "total_segments": len(segments),
                    }

        scored_candidates: List[Dict[str, Any]] = []

        # Find panel image files
        panel_files = sorted(
            [f for f in panels_dir.glob("*.jpg") if not f.name.startswith(".")] +
            [f for f in panels_dir.glob("*.png") if not f.name.startswith(".")] +
            [f for f in panels_dir.glob("*.webp") if not f.name.startswith(".")]
        )

        if not panel_files:
            # Fallback: check raw files
            raw_dir = chap_dir / "raw"
            if raw_dir.exists():
                panel_files = sorted([f for f in raw_dir.glob("*") if f.suffix.lower() in {".jpg", ".png", ".webp"}])

        if not panel_files:
            logger.warning(f"No panel files found for thumbnail candidate evaluation in {chap_dir}")
            return ThumbnailCandidatesDocument(
                project_slug=project_slug,
                chapter_number=str(chapter_number),
            )

        for p_idx, p_file in enumerate(panel_files):
            pid = p_file.stem  # e.g. "panel_001"
            seg_info = panel_seg_map.get(pid, {})
            score, features, emotion_tag, reason, reco_text = self._score_panel(
                p_file, seg_info, p_idx, len(panel_files)
            )
            scored_candidates.append({
                "panel_file": p_file,
                "panel_id": pid,
                "score": score,
                "features": features,
                "emotion_tag": emotion_tag,
                "reason": reason,
                "reco_text": reco_text,
                "focus_box": seg_info.get("focus_box") or (0.1, 0.1, 0.9, 0.9),
                "segment_index": seg_info.get("segment_index", p_idx),
            })

        # Sort descending by score
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)

        # Select Top 3 diverse candidates
        top_candidates = self._select_diverse_top3(scored_candidates)

        # Render variants using Pillow
        thumbnails_dir = chap_dir / "thumbnails"
        thumbnails_dir.mkdir(parents=True, exist_ok=True)

        out_candidates: List[ThumbnailCandidate] = []
        for rank_idx, item in enumerate(top_candidates, start=1):
            cand_id = f"cand_{rank_idx:02d}"
            clean_name = f"{cand_id}_clean.jpg"
            text_name = f"{cand_id}_text.jpg"
            clean_path = thumbnails_dir / clean_name
            text_path = thumbnails_dir / text_name

            ThumbnailComposer.compose_thumbnail(
                source_image_path=item["panel_file"],
                output_clean_path=clean_path,
                output_text_path=text_path,
                focus_box=item["focus_box"],
                overlay_text=item["reco_text"],
                text_safe_zone=item["features"].recommended_text_zone,
            )

            rel_source = str(item["panel_file"].relative_to(proj_dir)) if proj_dir in item["panel_file"].parents else item["panel_file"].name
            rel_clean = str(clean_path.relative_to(proj_dir)) if proj_dir in clean_path.parents else str(clean_path)
            rel_text = str(text_path.relative_to(proj_dir)) if proj_dir in text_path.parents else str(text_path)

            cand = ThumbnailCandidate(
                candidate_id=cand_id,
                source_panel=item["panel_file"].name,
                source_path=rel_source,
                focus_box=item["focus_box"],
                ranking_score=round(item["score"], 3),
                narrative_reason=item["reason"],
                emotion_tag=item["emotion_tag"],
                text_overlay_recommendation=item["reco_text"],
                composition=item["features"],
                provenance={
                    "chapter_number": str(chapter_number),
                    "panel_id": item["panel_id"],
                    "segment_index": item["segment_index"],
                },
                image_variant_clean=rel_clean,
                image_variant_text=rel_text,
                status="selected" if rank_idx == 1 else "proposed",
            )
            out_candidates.append(cand)

        doc = ThumbnailCandidatesDocument(
            project_slug=project_slug,
            chapter_number=str(chapter_number),
            selected_candidate_id="cand_01",
            candidates=out_candidates,
        )

        # Save to chapter directory
        doc_path = chap_dir / "thumbnail_candidates.json"
        atomic_write_json(doc_path, doc)

        # Stage top candidate to output/<slug>/chapter_<num>_thumbnail.jpg
        out_dir = get_output_dir(project_slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        staged_thumb = out_dir / f"chapter_{chapter_number}_thumbnail.jpg"
        top_clean_file = thumbnails_dir / "cand_01_clean.jpg"
        if top_clean_file.exists():
            shutil.copy2(top_clean_file, staged_thumb)

        logger.info(f"Thumbnail Director evaluated {len(out_candidates)} candidates staged to {staged_thumb}")
        return doc

    def _score_panel(
        self,
        panel_file: Path,
        seg_info: Dict[str, Any],
        panel_idx: int,
        total_panels: int,
    ) -> Tuple[float, CompositionFeatures, str, str, str]:
        """Calculates multi-factor heuristic ranking score S in [0.0, 1.0]."""
        narrator_text = seg_info.get("narrator_text", "")
        focus_box = seg_info.get("focus_box") or (0.1, 0.1, 0.9, 0.9)

        # 1. Emotional intensity E in [0.0, 1.0]
        e_score = 0.5
        detected_emotion = "action"
        low_text = narrator_text.lower()

        for tag, keywords in self.EMOTION_KEYWORDS.items():
            if any(k in low_text for k in keywords):
                e_score = 0.9
                detected_emotion = tag
                break

        # Bonus for auditory cues
        if any(cue in narrator_text for cue in ["[cười]", "[thở dài]", "[hét]", "[gầm]", "[kinh ngạc]"]):
            e_score = min(1.0, e_score + 0.15)

        # 2. Subject Visibility V in [0.0, 1.0]
        fx1, fy1, fx2, fy2 = focus_box
        area = max(0.01, (fx2 - fx1) * (fy2 - fy1))
        if 0.15 <= area <= 0.65:
            v_score = 1.0
        elif 0.08 <= area <= 0.85:
            v_score = 0.75
        else:
            v_score = 0.5

        # 3. Panel Resolution R in [0.0, 1.0]
        r_score = 0.8
        try:
            with Image.open(panel_file) as img:
                pw, ph = img.size
                r_score = min(1.0, (pw * ph) / (1080.0 * 1080.0))
        except Exception:
            pass

        # 4. Safe-Zone Clearance Z in [0.2, 1.0]
        # Bottom-right 25% width x 20% height holds YouTube duration badge
        center_x = (fx1 + fx2) / 2.0
        center_y = (fy1 + fy2) / 2.0
        safe_clear = not (center_x >= 0.75 and center_y >= 0.80)
        z_score = 1.0 if safe_clear else 0.20

        # 5. Narrative / Climax Weight N in [0.0, 1.0]
        # Later panels in chapter receive climax boost
        pos_ratio = panel_idx / float(max(1, total_panels))
        n_score = 0.5 + 0.3 * pos_ratio
        if '"' in narrator_text or '“' in narrator_text:
            n_score = min(1.0, n_score + 0.20)

        total_score = (
            0.25 * e_score +
            0.25 * v_score +
            0.20 * r_score +
            0.15 * z_score +
            0.15 * n_score
        )

        features = CompositionFeatures(
            subject_visibility=round(v_score, 2),
            facial_clarity=round(e_score, 2),
            safe_zone_clear=safe_clear,
            recommended_text_zone=TextSafeZone.TOP_LEFT if center_y > 0.4 else TextSafeZone.BOTTOM_LEFT,
            contrast_ratio=round(r_score, 2),
        )

        # Formulate reason & overlay recommendation
        if detected_emotion == "climax":
            reason = "Khoảnh khắc cao trào bộc phát chân khí hoặc chuyển biến cốt truyện quyết định"
            reco_text = "THỨC TỈNH BẤT NGỜ!"
        elif detected_emotion == "shock":
            reason = "Cận cảnh cảm xúc sững sờ kinh ngạc lôi cuốn người xem click"
            reco_text = "AI CHO PHÉP?!"
        elif detected_emotion == "confrontation":
            reason = "Cảnh đối đầu trực diện nghẹt thở giữa các nhân vật trọng tâm"
            reco_text = "DÁM ĐỐI ĐẦU TA?!"
        else:
            reason = "Hình ảnh hành động gay cấn thể hiện rõ nét chủ thể và không gian"
            reco_text = "QUYẾT CHIẾN ĐỈNH CAO!"

        return total_score, features, detected_emotion, reason, reco_text

    def _select_diverse_top3(self, scored_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Selects Top 3 diverse candidates ensuring variety in emotional tags and scene positions."""
        if len(scored_items) <= 3:
            return scored_items

        selected: List[Dict[str, Any]] = [scored_items[0]]
        used_emotions = {scored_items[0]["emotion_tag"]}
        used_panels = {scored_items[0]["panel_id"]}

        # Candidate 2: distinct emotion or distinct position
        for item in scored_items[1:]:
            if item["panel_id"] not in used_panels and item["emotion_tag"] not in used_emotions:
                selected.append(item)
                used_emotions.add(item["emotion_tag"])
                used_panels.add(item["panel_id"])
                break

        # Candidate 3: highest remaining score not in used_panels
        for item in scored_items[1:]:
            if item["panel_id"] not in used_panels:
                selected.append(item)
                used_panels.add(item["panel_id"])
                if len(selected) == 3:
                    break

        # If still < 3, pad with whatever is available
        while len(selected) < 3 and len(selected) < len(scored_items):
            for item in scored_items:
                if item not in selected:
                    selected.append(item)
                    break

        return selected


thumbnail_director = ThumbnailCandidateDirector()
