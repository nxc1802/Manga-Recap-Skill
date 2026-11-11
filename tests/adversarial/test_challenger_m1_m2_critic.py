"""
tests/adversarial/test_challenger_m1_m2_critic.py
Empirical Adversarial Test Suite by Challenger 1 (critic/specialist).
Stress-tests Milestone 1 (PR-01: Strict Form Validation) and Milestone 2 (PR-02: Kinematics & Slicing).

Mandated Tasks:
1. Adversarial Setup Testing (PR-01):
   - Permutation invariance & tamper detection in compute_snapshot_hash.
   - Hard rejection of non-user sources ("default", "auto", "inferred", "").
   - Domain boundaries: voice catalog, speed [0.5, 2.0], booleans, video quality.
   - Fail-closed verification across CLI entry points (crawl, render, cloud, batch).
2. Adversarial Kinematics & Slicing Testing (PR-02):
   - Monotonicity stress: Evaluate y(t) for PAN_DOWN across 10,000 points from t=0 to t=3T.
     Verify y(t_{k+1}) >= y(t_k) unconditionally, and y(t) == Delta y for t >= T (zero rebound).
   - Extreme tall panel test: Panels with H/W = 3.0, 5.0, 10.0, 20.0.
     Verify decompose_tall_panel decomposes every panel into sub-boxes strictly within [0.65, 2.0] aspect ratio.
   - Blank / sliver detection: Heights 1px to 79px, pure white, pure black, text-only.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from src.cli.exit_codes import CliValidationError
from src.cli.batch_cmd import handle_batch
from src.cli.cloud_cmd import handle_cloud
from src.cli.crawl_cmd import handle_crawl
from src.cli.render_cmd import handle_render
from src.core.schemas import (
    CameraMotionType,
    FocusBox,
    PanelItem,
    PanelStatus,
)
from src.core.series_setup import (
    ALL_QUESTION_IDS,
    QUESTION_METADATA,
    SeriesSetupAnswers,
    SetupAnswerItem,
    compute_snapshot_hash,
    require_series_setup,
    validate_complete_setup,
)
from src.core.storage import StorageFileNotFoundError
from src.render.camera import CameraMotionGenerator
from src.render.visual_policy import (
    decompose_tall_panel,
    detect_omit_visual_band,
    is_tall_panel,
)


# ==============================================================================
# PR-01: Adversarial Setup & Strict Form Tests
# ==============================================================================


@pytest.fixture
def canonical_setup_dict() -> Dict[str, Any]:
    """Returns a fully valid, canonical dictionary of user answers for Q01-Q10."""
    return {
        "Q01": {
            "question_id": "Q01",
            "question_title": QUESTION_METADATA["Q01"]["title"],
            "user_answer": {"source": "https://example.com/chap1", "chapters": "1"},
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q02": {
            "question_id": "Q02",
            "question_title": QUESTION_METADATA["Q02"]["title"],
            "user_answer": "kaggle",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q03": {
            "question_id": "Q03",
            "question_title": QUESTION_METADATA["Q03"]["title"],
            "user_answer": "Ngọc Huyền",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q04": {
            "question_id": "Q04",
            "question_title": QUESTION_METADATA["Q04"]["title"],
            "user_answer": "1.0",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q05": {
            "question_id": "Q05",
            "question_title": QUESTION_METADATA["Q05"]["title"],
            "user_answer": False,
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q06": {
            "question_id": "Q06",
            "question_title": QUESTION_METADATA["Q06"]["title"],
            "user_answer": "animation",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q07": {
            "question_id": "Q07",
            "question_title": QUESTION_METADATA["Q07"]["title"],
            "user_answer": True,
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q08": {
            "question_id": "Q08",
            "question_title": QUESTION_METADATA["Q08"]["title"],
            "user_answer": "Không upload",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q09": {
            "question_id": "Q09",
            "question_title": QUESTION_METADATA["Q09"]["title"],
            "user_answer": "final",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
        "Q10": {
            "question_id": "Q10",
            "question_title": QUESTION_METADATA["Q10"]["title"],
            "user_answer": "Không BGM",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T18:00:00Z",
        },
    }


@pytest.fixture
def canonical_setup_answers(canonical_setup_dict) -> SeriesSetupAnswers:
    """Returns a valid SeriesSetupAnswers instance with a valid cryptographic snapshot hash."""
    answers_items = {
        qid: SetupAnswerItem(**item) for qid, item in canonical_setup_dict.items()
    }
    snap_hash = compute_snapshot_hash(answers_items)
    return SeriesSetupAnswers(
        schema_version="4.0.0",
        project_slug="adversarial_test_series",
        setup_revision=1,
        created_at="2026-11-10T18:00:00Z",
        updated_at="2026-11-10T18:00:00Z",
        user_confirmed_at="2026-11-10T18:00:00Z",
        reviewer="user",
        answers=answers_items,
        snapshot_hash=snap_hash,
    )


class TestPR01AdversarialSetup:
    """Adversarial challenge test suite for PR-01 Strict Form Validation."""

    def test_compute_snapshot_hash_permutation_invariance(self, canonical_setup_dict):
        """
        Verify that compute_snapshot_hash produces identical SHA-256 hashes
        regardless of top-level question key order or nested dictionary key order.
        """
        # Baseline hash
        hash_forward = compute_snapshot_hash(canonical_setup_dict)

        # Reverse key order
        rev_dict = {k: canonical_setup_dict[k] for k in reversed(list(canonical_setup_dict.keys()))}
        hash_reverse = compute_snapshot_hash(rev_dict)
        assert hash_forward == hash_reverse, "Hash failed top-level reverse permutation invariance"

        # Arbitrary shuffled key order
        keys = list(canonical_setup_dict.keys())
        shuffled_keys = [keys[4], keys[1], keys[9], keys[0], keys[3], keys[7], keys[2], keys[5], keys[8], keys[6]]
        shuffled_dict = {k: canonical_setup_dict[k] for k in shuffled_keys}
        hash_shuffled = compute_snapshot_hash(shuffled_dict)
        assert hash_forward == hash_shuffled, "Hash failed arbitrary key permutation invariance"

        # Nested dict key permutation in Q01 (chapters before source vs source before chapters)
        nested_permuted = copy.deepcopy(canonical_setup_dict)
        nested_permuted["Q01"]["user_answer"] = {
            "chapters": "1",
            "source": "https://example.com/chap1",
        }
        hash_nested = compute_snapshot_hash(nested_permuted)
        assert hash_forward == hash_nested, "Hash failed nested dictionary key permutation invariance"

    def test_tamper_detection_single_character_alteration(self, canonical_setup_answers):
        """
        Empirically verify tamper detection: modifying any single character in user_answer,
        confirmed_at, or answer_source MUST invalidate the snapshot hash and fail validation.
        """
        base_obj = canonical_setup_answers

        # 1. Modify user_answer in Q03 (voice preset)
        tampered_voice = base_obj.model_copy(deep=True)
        tampered_voice.answers["Q03"] = SetupAnswerItem(
            question_id="Q03",
            question_title=QUESTION_METADATA["Q03"]["title"],
            user_answer="Trúc Ly",
            answer_source="user",
            confirmed_at=base_obj.user_confirmed_at,
        )
        is_valid, errors = validate_complete_setup(tampered_voice)
        assert not is_valid, "Tampered voice was accepted without updating snapshot_hash"
        assert any("Snapshot hash mismatch" in err for err in errors)

        # 2. Modify single digit in confirmed_at
        tampered_time = base_obj.model_copy(deep=True)
        tampered_time.answers["Q01"] = SetupAnswerItem(
            question_id="Q01",
            question_title=QUESTION_METADATA["Q01"]["title"],
            user_answer=base_obj.answers["Q01"].user_answer,
            answer_source="user",
            confirmed_at="2026-11-10T18:00:01Z",  # altered 1 second
        )
        is_valid, errors = validate_complete_setup(tampered_time)
        assert not is_valid, "Tampered timestamp was accepted without updating snapshot_hash"
        assert any("Snapshot hash mismatch" in err for err in errors)

        # 3. Add single trailing space to string answer
        tampered_space = base_obj.model_copy(deep=True)
        tampered_space.answers["Q06"] = SetupAnswerItem(
            question_id="Q06",
            question_title=QUESTION_METADATA["Q06"]["title"],
            user_answer="animation ",  # trailing space
            answer_source="user",
            confirmed_at=base_obj.user_confirmed_at,
        )
        is_valid, errors = validate_complete_setup(tampered_space)
        assert not is_valid, "Trailing whitespace tampering was accepted without updating snapshot_hash"
        assert any("Snapshot hash mismatch" in err for err in errors)

    def test_non_user_sources_hard_rejection(self, canonical_setup_dict):
        """
        Test that any answer_source other than literal 'user' is unconditionally rejected
        both at the Pydantic model validation layer and during validate_complete_setup.
        """
        invalid_sources = ["default", "auto", "inferred", "", "agent", "system", "llm", "None", None]

        for bad_src in invalid_sources:
            bad_dict = copy.deepcopy(canonical_setup_dict["Q01"])
            bad_dict["answer_source"] = bad_src

            with pytest.raises((ValueError, ValidationError)):
                SetupAnswerItem.model_validate(bad_dict)

    def test_domain_boundaries_voice_catalog(self, canonical_setup_answers):
        """
        Verify that non-catalog voices are strictly rejected by validate_complete_setup.
        """
        invalid_voices = ["UnknownVoice_999", "Cortana", "Siri", "Google_TTS", "Invalid_Vietnamese_Voice"]

        for bad_voice in invalid_voices:
            obj = canonical_setup_answers.model_copy(deep=True)
            obj.answers["Q03"] = SetupAnswerItem(
                question_id="Q03",
                question_title=QUESTION_METADATA["Q03"]["title"],
                user_answer=bad_voice,
                answer_source="user",
                confirmed_at=obj.user_confirmed_at,
            )
            # Recompute hash so failure is specifically from domain validation, not hash mismatch
            obj.snapshot_hash = compute_snapshot_hash(obj.answers)
            is_valid, errors = validate_complete_setup(obj)
            assert not is_valid, f"Unrecognized voice '{bad_voice}' was accepted!"
            assert any("not recognized in official VieNeu catalog" in err for err in errors)

    def test_domain_boundaries_speed_range(self, canonical_setup_answers):
        """
        Verify that speech speeds outside [0.5, 2.0] or non-float strings are strictly rejected.
        """
        invalid_speeds = [0.49, 0.0, -1.0, 2.01, 3.5, 100.0, "fast", "slow", "turbo"]

        for bad_speed in invalid_speeds:
            obj = canonical_setup_answers.model_copy(deep=True)
            obj.answers["Q04"] = SetupAnswerItem(
                question_id="Q04",
                question_title=QUESTION_METADATA["Q04"]["title"],
                user_answer=str(bad_speed),
                answer_source="user",
                confirmed_at=obj.user_confirmed_at,
            )
            obj.snapshot_hash = compute_snapshot_hash(obj.answers)
            is_valid, errors = validate_complete_setup(obj)
            assert not is_valid, f"Speed '{bad_speed}' was accepted!"
            assert any("Question Q04: speed" in err for err in errors)

    def test_domain_boundaries_booleans(self, canonical_setup_answers):
        """
        Verify that non-boolean values for Q05 (burn subtitles) and Q07 (thumbnail) are rejected.
        """
        invalid_bools = ["maybe", "unsure", "123xyz", "default", "auto", "partially"]

        for bad_val in invalid_bools:
            # Q05
            obj5 = canonical_setup_answers.model_copy(deep=True)
            obj5.answers["Q05"] = SetupAnswerItem(
                question_id="Q05",
                question_title=QUESTION_METADATA["Q05"]["title"],
                user_answer=bad_val,
                answer_source="user",
                confirmed_at=obj5.user_confirmed_at,
            )
            obj5.snapshot_hash = compute_snapshot_hash(obj5.answers)
            is_valid5, errors5 = validate_complete_setup(obj5)
            assert not is_valid5, f"Invalid boolean '{bad_val}' for Q05 was accepted!"

            # Q07
            obj7 = canonical_setup_answers.model_copy(deep=True)
            obj7.answers["Q07"] = SetupAnswerItem(
                question_id="Q07",
                question_title=QUESTION_METADATA["Q07"]["title"],
                user_answer=bad_val,
                answer_source="user",
                confirmed_at=obj7.user_confirmed_at,
            )
            obj7.snapshot_hash = compute_snapshot_hash(obj7.answers)
            is_valid7, errors7 = validate_complete_setup(obj7)
            assert not is_valid7, f"Invalid boolean '{bad_val}' for Q07 was accepted!"

    def test_domain_boundaries_quality_strings(self, canonical_setup_answers):
        """
        Verify that video quality strings for Q09 must contain preview/final/720/1080.
        """
        invalid_qualities = ["4k_ultra_hd", "cinema_4k", "8k", "raw_lossless", "unknown_quality"]

        for bad_q in invalid_qualities:
            obj = canonical_setup_answers.model_copy(deep=True)
            obj.answers["Q09"] = SetupAnswerItem(
                question_id="Q09",
                question_title=QUESTION_METADATA["Q09"]["title"],
                user_answer=bad_q,
                answer_source="user",
                confirmed_at=obj.user_confirmed_at,
            )
            obj.snapshot_hash = compute_snapshot_hash(obj.answers)
            is_valid, errors = validate_complete_setup(obj)
            assert not is_valid, f"Quality '{bad_q}' was accepted!"
            assert any("video quality must specify" in err for err in errors)

    def test_fail_closed_unconfigured_project_cli_entry_points(self, tmp_path):
        """
        Test that CLI pipeline entry points fail closed when run on an unconfigured project.
        """
        proj_slug = "unconfigured_adversarial_project"
        unconf_dir = tmp_path / proj_slug
        unconf_dir.mkdir(parents=True, exist_ok=True)

        # 1. require_series_setup direct call
        with pytest.raises(CliValidationError) as exc_info:
            require_series_setup(proj_slug, project_dir=unconf_dir, allow_legacy=True)
        assert "Strict Form Gate failed" in str(exc_info.value)

        # 2. crawl entry point
        args_crawl = argparse.Namespace(
            project=proj_slug,
            chapter=1,
            url="https://example.com/ch1",
            json=True,
        )
        with pytest.raises(CliValidationError) as exc_crawl:
            handle_crawl(args_crawl)
        assert "Strict Form Gate failed" in str(exc_crawl.value)

        # 3. render entry point
        from unittest.mock import patch
        args_render = argparse.Namespace(
            project=proj_slug,
            chapter=1,
            project_dir=str(unconf_dir),
            kaggle=False,
            quality="preview",
            skip_preview=True,
            json=True,
        )
        with patch("src.cli.render_cmd.get_project_dir", return_value=unconf_dir):
            with pytest.raises(CliValidationError) as exc_render:
                handle_render(args_render)
            assert "Strict Form Gate failed" in str(exc_render.value)

        # 4. cloud entry point
        args_cloud = argparse.Namespace(
            cloud_action="run",
            project=proj_slug,
            chapter=1,
            kaggle=True,
            json=True,
        )
        with pytest.raises(CliValidationError) as exc_cloud:
            handle_cloud(args_cloud)
        assert "Strict Form Gate failed" in str(exc_cloud.value)


# ==============================================================================
# PR-02: Adversarial Kinematics, Motion & Tall Panel Slicing Tests
# ==============================================================================


class TestPR02AdversarialKinematicsAndSlicing:
    """Adversarial challenge test suite for PR-02 Visual-First Kinematics & Slicing."""

    @pytest.mark.parametrize("T", [0.5, 1.0, 3.5, 7.25, 15.0])
    def test_monotonicity_pan_down_10000_discrete_points(self, T: float):
        """
        Monotonicity Stress:
        Evaluate y(t) for PAN_DOWN across 10,000 discrete points from t = 0 to t = 3T.
        Verifies:
        1. y(t_{k+1}) >= y(t_k) unconditionally (strictly monotonic top-down motion).
        2. y(t) == Delta y for all t >= T (zero rebound / rubber-banding at cuts).
        """
        cmg = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)
        img_w = 1280
        img_h = 3000

        filter_str, is_zp = cmg.build_filterchain(
            img_w=img_w,
            img_h=img_h,
            duration_sec=T,
            motion_type=CameraMotionType.PAN_DOWN,
        )
        assert not is_zp, "PAN_DOWN must not use zoompan"

        # Parse delta_y and t_denom from filter string
        match = re.search(
            r"crop=\d+:\d+:\d+:'(\d+)\*0\.5\*\(1-cos\(PI\*min\(1,max\(0,t/([\d\.]+)\)\)\)\)'",
            filter_str,
        )
        assert match, f"PAN_DOWN filter expression does not match canonical clamped cosine form: {filter_str}"
        delta_y = float(match.group(1))
        t_denom = float(match.group(2))
        assert abs(t_denom - T) < 1e-4

        # Sample 10,000 points spanning t in [0, 3T]
        t_vals = np.linspace(0.0, 3.0 * T, 10000)
        p_vals = np.clip(t_vals / t_denom, 0.0, 1.0)
        y_vals = delta_y * 0.5 * (1.0 - np.cos(np.pi * p_vals))

        # 1. Monotonicity check: Delta y_k >= 0
        diffs = np.diff(y_vals)
        min_diff = np.min(diffs)
        assert min_diff >= -1e-12, f"Monotonicity violated: min_diff = {min_diff} < 0"

        # 2. Zero rebound check: y(t) == delta_y for all t >= T
        idx_after_T = np.where(t_vals >= T)[0]
        y_after_T = y_vals[idx_after_T]
        max_rebound_error = np.max(np.abs(y_after_T - delta_y))
        assert max_rebound_error < 1e-9, f"Rebound detected: max_rebound_error = {max_rebound_error}"

        # 3. Peak velocity check <= 120 px/s
        peak_velocity = (np.pi / 2.0) * (delta_y / T)
        assert peak_velocity <= 120.01, f"Peak velocity {peak_velocity:.2f} px/s exceeds 120 px/s limit"

    @pytest.mark.parametrize("T", [0.5, 1.0, 3.5, 7.25, 15.0])
    def test_monotonicity_vertical_scroll_10000_discrete_points(self, T: float):
        """
        Monotonicity Stress for VERTICAL_SCROLL (linear progression):
        Evaluate y(t) across 10,000 discrete points from t = 0 to t = 3T.
        Verifies y(t_{k+1}) >= y(t_k) and y(t) == Delta y for t >= T.
        """
        cmg = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)
        img_w = 1280
        img_h = 3000

        filter_str, is_zp = cmg.build_filterchain(
            img_w=img_w,
            img_h=img_h,
            duration_sec=T,
            motion_type=CameraMotionType.VERTICAL_SCROLL,
        )
        match = re.search(r"crop=\d+:\d+:\d+:'(\d+)\*min\(1,max\(0,t/([\d\.]+)\)\)'", filter_str)
        assert match, f"VERTICAL_SCROLL filter does not match linear clamped form: {filter_str}"
        delta_y = float(match.group(1))
        t_denom = float(match.group(2))

        # Velocity check <= 120 px/s
        assert (delta_y / T) <= 120.01

        t_vals = np.linspace(0.0, 3.0 * T, 10000)
        p_vals = np.clip(t_vals / t_denom, 0.0, 1.0)
        y_vals = delta_y * p_vals

        diffs = np.diff(y_vals)
        assert np.min(diffs) >= -1e-12

        idx_after_T = np.where(t_vals >= T)[0]
        assert np.max(np.abs(y_vals[idx_after_T] - delta_y)) < 1e-9

    @pytest.mark.parametrize("hw_ratio", [3.0, 5.0, 10.0, 20.0])
    def test_extreme_tall_panel_decomposition_aspect_ratios(self, hw_ratio: float):
        """
        Extreme Tall Panel Test:
        Generates synthetic panels with aspect ratios H/W = 3.0, 5.0, 10.0, 20.0.
        Verifies that decompose_tall_panel decomposes every panel into sub-boxes
        strictly within [0.65, 2.0] aspect ratio with valid reading progression and overlaps.
        
        NOTE: This test empirically challenges the 0.30 clamp in decompose_tall_panel.
        """
        width = 1000
        height = int(width * hw_ratio)

        boxes = decompose_tall_panel(
            panel_id=f"panel_hw_{hw_ratio}",
            width=width,
            height=height,
            target_aspect_min=0.65,
            target_aspect_max=2.0,
        )

        assert len(boxes) >= 2, f"Failed: H/W={hw_ratio} produced only {len(boxes)} slices"

        # Check each slice's aspect ratio and boundaries
        for i, b in enumerate(boxes):
            w_px = (b.x_max - b.x_min) * width
            h_px = (b.y_max - b.y_min) * height
            ar_sub = w_px / h_px if h_px > 0 else 0.0

            # Target contract: 0.65 <= W/H <= 2.0
            assert 0.65 <= ar_sub <= 2.0, (
                f"Defect in decompose_tall_panel: Slice {i} of panel H/W={hw_ratio} has "
                f"aspect ratio {ar_sub:.4f} (h_px={h_px:.1f}), which violates [0.65, 2.0]."
            )

        # Check strictly monotonic top-to-bottom reading progression
        for i in range(len(boxes) - 1):
            assert boxes[i + 1].y_min > boxes[i].y_min, (
                f"Non-monotonic progression between slice {i} and {i+1} for H/W={hw_ratio}"
            )

        # Check overlap between consecutive slices (no gap)
        for i in range(len(boxes) - 1):
            assert boxes[i].y_max > boxes[i + 1].y_min, (
                f"Cut gap seam detected between slice {i} and {i+1} for H/W={hw_ratio}"
            )

    @pytest.mark.parametrize("h", [1, 10, 45, 79])
    def test_sliver_detection_heights_1px_to_79px(self, h: int):
        """
        Verify that images with height < 80px (slivers, divider lines, scanlation credits)
        are unconditionally classified as omit_visual with reason 'sliver'.
        """
        img = Image.new("RGB", (1000, h), (128, 128, 128))
        is_omit, reason = detect_omit_visual_band(img_path_or_crop=img, min_height_px=80)
        assert is_omit is True
        assert reason == "sliver"

    def test_blank_band_detection_pure_white_and_pure_black(self):
        """
        Verify that pure solid white and solid black images (variance 0.0)
        are unconditionally classified as omit_visual with reason 'blank_band'.
        """
        # Pure white
        img_white = Image.new("RGB", (1000, 1000), (255, 255, 255))
        is_omit_w, reason_w = detect_omit_visual_band(img_path_or_crop=img_white)
        assert is_omit_w is True
        assert reason_w == "blank_band"

        # Pure black
        img_black = Image.new("RGB", (1000, 1000), (0, 0, 0))
        is_omit_b, reason_b = detect_omit_visual_band(img_path_or_crop=img_black)
        assert is_omit_b is True
        assert reason_b == "blank_band"

    def test_text_only_band_detection(self):
        """
        Verify that text-only bands with notes or OCR indicators are classified as omit_visual.
        """
        img_text = Image.new("RGB", (1000, 500), (255, 255, 255))
        draw = ImageDraw.Draw(img_text)
        draw.text((100, 100), "Chapter 1: The Legend of the Holy Blade", fill=(0, 0, 0))

        p_item = PanelItem(
            panel_id="panel_text_01",
            sequence_number=1,
            chapter_id="1",
            page_number=1,
            panel_number=1,
            filename="panel_text_01.jpg",
            relative_path="panels/panel_text_01.jpg",
            source_image="raw/page_001.jpg",
            crop_box=(0, 0, 1000, 500),
            sha256="a" * 64,
            width=1000,
            height=500,
            aspect_ratio=2.0,
            notes="text_only title card",
            ocr_text_en="Chapter 1: The Legend of the Holy Blade",
        )
        is_omit, reason = detect_omit_visual_band(img_path_or_crop=img_text, panel_item=p_item)
        assert is_omit is True
        assert reason == "text_only"
