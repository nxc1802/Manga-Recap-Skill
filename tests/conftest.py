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


# ==============================================================================
# P13 Standardized Test Pyramid Partitioning
# ==============================================================================

PYRAMID_LAYERS = ("ci_l1", "ci_l2", "ci_l3", "live_l4", "perf")
DETERMINISTIC_LAYERS = ("ci_l1", "ci_l2", "ci_l3")

# Nodeid prefix overrides for files containing tests at multiple pyramid levels.
PYRAMID_NODE_PREFIX_LAYERS = {
    "tests/unit/test_p14_state_machine.py::TestCanonicalStateMachineUnit": "ci_l1",
    "tests/unit/test_p14_state_machine.py::TestCanonicalStateMachineL2AndBypasses": "ci_l2",
    "tests/unit/test_artifact_correctness.py::test_encoder_produces_decodable_frames_and_rejects_absent_audio": "ci_l3",
    "tests/unit/test_artifact_correctness.py::test_real_encoded_blank_video_is_rejected": "ci_l3",
    "tests/unit/test_artifact_correctness.py::": "ci_l1",
}

# Complete file-level partition of deterministic tests under tests/.
PYRAMID_FILE_LAYERS = {
    # --- CI-L1: schemas, VisualPlan, setup, state machine, receipts, scheduler, GPU assignment, pure unit logic ---
    "tests/test_tts_parser.py": "ci_l1",
    "tests/test_utils.py": "ci_l1",
    "tests/adversarial/test_challenger_m1_m2_critic.py": "ci_l1",
    "tests/adversarial/test_cloud_p0_adversarial.py": "ci_l1",
    "tests/adversarial/test_m1_adversarial.py": "ci_l1",
    "tests/adversarial/test_m3_segmentation_adversarial.py": "ci_l1",
    "tests/unit/cloud/test_kaggle_p0_correctness.py": "ci_l1",
    "tests/unit/cloud/test_kaggle_service.py": "ci_l1",
    "tests/unit/test_agentic_skill.py": "ci_l1",
    "tests/unit/test_audio_mixer.py": "ci_l1",
    "tests/unit/test_camera.py": "ci_l1",
    "tests/unit/test_cli.py": "ci_l1",
    "tests/unit/test_contact_sheet.py": "ci_l1",
    "tests/unit/test_lab_interface.py": "ci_l1",
    "tests/unit/test_p13_test_pyramid.py": "ci_l1",
    "tests/unit/test_phase4_workspace_export.py": "ci_l1",
    "tests/unit/test_phase_b.py": "ci_l1",
    "tests/unit/test_pipeline_regression.py": "ci_l1",
    "tests/unit/test_review_pack.py": "ci_l1",
    "tests/unit/test_schemas.py": "ci_l1",
    "tests/unit/test_series_setup.py": "ci_l1",
    "tests/unit/test_skill_spec.py": "ci_l1",
    "tests/unit/test_storage.py": "ci_l1",
    "tests/unit/test_streaming_segmenter.py": "ci_l1",
    "tests/unit/test_subtitle.py": "ci_l1",
    "tests/unit/test_synthetic_fixture.py": "ci_l1",
    "tests/unit/test_visual_policy.py": "ci_l1",
    "tests/unit/test_voice_catalog.py": "ci_l1",

    # --- CI-L2: script->TTS, audio->VisualPlan, VisualPlan->renderer contracts, cloud package->sync, Dual-T4 queue->worker, HF/YouTube/cleanup ---
    "tests/adversarial/test_challenger_m3_m4_adversarial.py": "ci_l2",
    "tests/adversarial/test_m2_adversarial.py": "ci_l2",
    "tests/adversarial/test_m2_cli_adversarial.py": "ci_l2",
    "tests/adversarial/test_m3_tts_adversarial.py": "ci_l2",
    "tests/adversarial/test_m3_tts_empirical_challenger2.py": "ci_l2",
    "tests/unit/cloud/test_canonical_cloud.py": "ci_l2",
    "tests/unit/cloud/test_p8_dual_t4_architecture.py": "ci_l2",
    "tests/unit/cloud/test_p9_benchmark_harness.py": "ci_l2",
    "tests/unit/test_canonical_workflow.py": "ci_l2",
    "tests/unit/test_clean_cmd.py": "ci_l2",
    "tests/unit/test_defects.py": "ci_l2",
    "tests/unit/test_hf_archive.py": "ci_l2",
    "tests/unit/test_p0_fixes.py": "ci_l2",
    "tests/unit/test_p1_fixes.py": "ci_l2",
    "tests/unit/test_p2_simplification_and_batch.py": "ci_l2",
    "tests/unit/test_p8_tts_optimization.py": "ci_l2",
    "tests/unit/test_phase_a.py": "ci_l2",
    "tests/unit/test_phase_c.py": "ci_l2",
    "tests/unit/test_safe_cleanup.py": "ci_l2",
    "tests/unit/test_tts_benchmark.py": "ci_l2",
    "tests/unit/test_tts_engine.py": "ci_l2",
    "tests/unit/test_v4_regression_migration.py": "ci_l2",
    "tests/unit/test_youtube_integration.py": "ci_l2",

    # --- CI-L3: real FFmpeg local media, E2E, fastpath, and render integration tests ---
    "tests/adversarial/test_challenger_m1_m2_empirical.py": "ci_l3",
    "tests/e2e/test_e2e_pipeline.py": "ci_l3",
    "tests/e2e/test_phase4_real_verification.py": "ci_l3",
    "tests/test_stitch_segment.py": "ci_l3",
    "tests/test_video_modes.py": "ci_l3",
    "tests/unit/test_motion_v4.py": "ci_l3",
    "tests/unit/test_p1_video_quality.py": "ci_l3",
    "tests/unit/test_p8_render_fastpath.py": "ci_l3",
    "tests/unit/test_v41_perf_optimizations.py": "ci_l3",
    "tests/unit/test_render_optimizations.py": "ci_l3",
    "tests/unit/test_video_engine.py": "ci_l3",
    "tests/unit/test_video_validator.py": "ci_l3",
    "tests/verification/test_matrix_verification.py": "ci_l3",
    "tests/verification/test_stabilization_matrix_t13_t21.py": "ci_l3",
}


def _resolve_pyramid_layer(nodeid: str, rel_path: str) -> str | None:
    for prefix, layer in PYRAMID_NODE_PREFIX_LAYERS.items():
        if nodeid.startswith(prefix):
            return layer
    return PYRAMID_FILE_LAYERS.get(rel_path)


def pytest_collection_modifyitems(config, items):
    root = Path(str(config.rootpath)).resolve()
    for item in items:
        existing = {m.name for m in item.iter_markers()} & set(PYRAMID_LAYERS)
        if len(existing) > 1:
            raise pytest.UsageError(
                f"Test {item.nodeid} has multiple pyramid layer markers: {sorted(existing)}"
            )
        if existing:
            continue

        item_path = Path(str(item.path)).resolve()
        try:
            rel_path = item_path.relative_to(root).as_posix()
        except ValueError:
            rel_path = item_path.as_posix()

        layer = _resolve_pyramid_layer(item.nodeid, rel_path)
        if layer is None:
            raise pytest.UsageError(
                f"Test {item.nodeid} ({rel_path}) has no pyramid layer marker. "
                f"Add @pytest.mark.<layer> ({', '.join(PYRAMID_LAYERS)}) or register it in tests/conftest.py."
            )
        item.add_marker(getattr(pytest.mark, layer))
