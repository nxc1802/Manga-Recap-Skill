"""Tests for Phase 4 Decision API httpx provider and domain panel routing."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from PIL import Image, ImageDraw

from src.core.decision_engine import (
    call_gemini_decision as compat_call_gemini_decision,
    run_decision_pipeline as compat_run_decision_pipeline,
)
from src.core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderIntegrityError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from src.core.project import init_chapter_layout, init_project_layout
from src.core.schemas import PanelItem, PanelManifest, PanelStatus
from src.core.storage import atomic_write_json, load_manifest, read_json
from src.ingest.decision import (
    DecisionPolicyConfig,
    build_panel_questions,
    compute_decision_cache_version,
    crop_edges_to_region,
    decide_single_panel,
    run_decision_pipeline,
)
from src.providers.decision import DECISIONS_ENDPOINT, DecisionClient


def _save_panel(path: Path, width: int, height: int, *, draw_gutter: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (width, height), color=(40, 60, 120))
    draw = ImageDraw.Draw(im)
    # Draw high-variance vertical stripes so candidate cut detector distinguishes art vs gap
    for x in range(0, width, 40):
        color = (20, 30, 80) if (x // 40) % 2 == 0 else (200, 140, 60)
        draw.rectangle([x, 0, min(width, x + 20), height], fill=color)
    if draw_gutter:
        mid = height // 2
        draw.rectangle([0, mid - 50, width, mid + 50], fill=(255, 255, 255))
    im.save(path, format="JPEG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_manifest_chapter(
    tmp_path: Path,
    monkeypatch,
    panels_spec: List[Dict[str, Any]],
) -> Path:
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    init_project_layout("dec-proj", "Decision Test Series")
    chap_dir = init_chapter_layout("dec-proj", "1")

    items: List[PanelItem] = []
    vcs_cursor = 0
    for idx, spec in enumerate(panels_spec, start=1):
        pid = spec["panel_id"]
        rel = f"panels/{pid}.jpg"
        raw_name = f"{idx:03d}.png"
        w, h = spec["width"], spec["height"]
        # Save matching raw page at target width 1080 so VCS coordinates match pixel bounds
        raw_path = chap_dir / "raw" / raw_name
        _save_panel(raw_path, 1080, h, draw_gutter=spec.get("draw_gutter", False))
        # Also save as PNG bytes cleanly via PIL
        with Image.open(raw_path) as im:
            im.save(raw_path, format="PNG")
        sha = _save_panel(chap_dir / rel, 1080, h, draw_gutter=spec.get("draw_gutter", False))
        v_start = vcs_cursor
        vcs_cursor += h
        items.append(
            PanelItem(
                panel_id=pid,
                sequence_number=idx,
                filename=f"{pid}.jpg",
                relative_path=rel,
                source_image=raw_name,
                crop_box=(0, 0, 1080, h),
                vcs_start=v_start,
                vcs_end=vcs_cursor,
                width=1080,
                height=h,
                aspect_ratio=round(1080 / float(h), 4),
                sha256=sha,
            )
        )

    atomic_write_json(
        chap_dir / "manifest.json",
        PanelManifest(
            project_slug="dec-proj",
            chapter_number="1",
            total_raw_images=len(items),
            total_panels=len(items),
            panels=items,
        ),
    )
    return chap_dir


def _four_sided_payload(
    *,
    panel_group: str = "group_1_story_art",
    structure_action: str = "ok_single",
    edges: tuple[int, int, int, int] = (0, 10, 0, 10),
    tokens: int = 100,
) -> Dict[str, Any]:
    def _edge_obj(pct: int) -> Dict[str, Any]:
        key = str(pct)
        probs = {opt: 0.01 for opt in ("0", "10", "20", "30", "40")}
        probs[key] = 0.90 if pct == 0 else 0.85
        if pct > 0:
            probs["0"] = 0.11
        return {"type": "choice", "choice": key, "confidence": probs[key], "probabilities": probs}

    return {
        "answers": {
            "panel_group": {
                "type": "choice",
                "choice": panel_group,
                "confidence": 0.95,
                "probabilities": {panel_group: 0.95},
            },
            "structure_action": {
                "type": "choice",
                "choice": structure_action,
                "confidence": 0.92,
                "probabilities": {structure_action: 0.92},
            },
            "crop_left": _edge_obj(edges[0]),
            "crop_top": _edge_obj(edges[1]),
            "crop_right": _edge_obj(edges[2]),
            "crop_bottom": _edge_obj(edges[3]),
        },
        "usage": {"total_input_tokens": tokens},
    }


def test_decision_client_retries_429_and_validates_response_and_redacts_secrets(tmp_path):
    img_path = tmp_path / "panel.jpg"
    _save_panel(img_path, 600, 800)
    _, questions = build_panel_questions(600, 800)
    assert {"panel_group", "structure_action", "crop_left", "crop_top", "crop_right", "crop_bottom"}.issubset(questions.keys())
    assert "motion_mode" not in questions
    secret = "AIzaSecretUnitTestKey987654321"
    sleeps: List[float] = []
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert str(request.url) == DECISIONS_ENDPOINT
        assert request.headers["x-goog-api-key"] == secret
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "1.5"},
                text=f"rate limited for {secret}",
            )
        return httpx.Response(200, json=_four_sided_payload(edges=(10, 0, 10, 0)))

    http_client = httpx.Client(transport=httpx.MockTransport(_handler))
    client = DecisionClient(max_retries=3, http_client=http_client, sleep_fn=sleeps.append)
    res = client.call(
        image_path=img_path,
        text_context="ctx",
        questions=questions,
        api_key=secret,
    )
    assert attempts == 2
    assert sleeps == [1.5]
    assert res["answers"]["crop_left"]["choice"] == "10"

    # Exhausted 429 raises ProviderRateLimitError with secret scrubbed
    exhaust_sleeps: List[float] = []

    def _always_429(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=f"quota exceeded key={secret}")

    exhaust_client = DecisionClient(
        max_retries=2,
        retry_base_seconds=0.5,
        retry_step_seconds=0.5,
        http_client=httpx.Client(transport=httpx.MockTransport(_always_429)),
        sleep_fn=exhaust_sleeps.append,
    )
    with pytest.raises(ProviderRateLimitError) as exc_info:
        exhaust_client.call(image_path=img_path, text_context="ctx", questions=questions, api_key=secret)
    assert exhaust_sleeps == [0.5]
    assert secret not in str(exc_info.value)
    assert "[REDACTED" in str(exc_info.value)


@pytest.mark.parametrize(
    ("status_code", "body", "exc_cls"),
    [
        (401, {"error": "unauthorized AIzaSecretLeak00001"}, ProviderAuthError),
        (403, {"error": "forbidden"}, ProviderAuthError),
        (400, {"error": "bad questions payload"}, ProviderError),
    ],
)
def test_decision_client_fail_fast_on_auth_and_client_errors(tmp_path, status_code, body, exc_cls):
    img_path = tmp_path / "panel.jpg"
    _save_panel(img_path, 400, 600)
    _, questions = build_panel_questions(400, 600)
    secret = "AIzaSecretLeak00001"
    calls = 0
    sleeps: List[float] = []

    def _handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json=body)

    client = DecisionClient(
        max_retries=5,
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
        sleep_fn=sleeps.append,
    )
    with pytest.raises(exc_cls) as exc_info:
        client.call(image_path=img_path, text_context="ctx", questions=questions, api_key=secret)
    assert calls == 1
    assert sleeps == []
    assert secret not in str(exc_info.value)


def test_decision_client_handles_timeout_and_invalid_responses(tmp_path):
    img_path = tmp_path / "panel.jpg"
    _save_panel(img_path, 400, 600)
    _, questions = build_panel_questions(400, 600)
    sleeps: List[float] = []
    calls = 0

    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    timeout_client = DecisionClient(
        max_retries=2,
        http_client=httpx.Client(transport=httpx.MockTransport(_timeout_handler)),
        sleep_fn=sleeps.append,
    )
    with pytest.raises(ProviderTimeoutError):
        timeout_client.call(image_path=img_path, text_context="ctx", questions=questions, api_key="key-123")
    assert calls == 2
    assert sleeps == [2.0]

    for bad_resp in (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=["unexpected_list"]),
        httpx.Response(200, json={"answers": {"panel_group": {"choice": "not_a_real_choice"}}}),
        httpx.Response(200, json={"answers": {}}),
    ):
        bad_client = DecisionClient(
            max_retries=1,
            http_client=httpx.Client(transport=httpx.MockTransport(lambda _r, resp=bad_resp: resp)),
        )
        with pytest.raises(ProviderIntegrityError):
            bad_client.call(image_path=img_path, text_context="ctx", questions=questions, api_key="key-123")


def test_pipeline_cache_hit_version_invalidation_and_partial_batch_resume(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 600, "height": 800},
            {"panel_id": "panel_002", "width": 600, "height": 800},
        ],
    )

    calls = 0

    def _fail_second_call(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json=_four_sided_payload(edges=(10, 0, 10, 0)))

    partial_client = DecisionClient(
        max_retries=1,
        http_client=httpx.Client(transport=httpx.MockTransport(_fail_second_call)),
    )
    with pytest.raises(ProviderRateLimitError):
        run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=partial_client)

    # Panel 1 succeeded and was persisted to decision_cache.json
    saved_cache = read_json(chap_dir / "decision_cache.json")
    assert "panel_001" in saved_cache
    assert saved_cache["panel_001"]["cache_version"] == compute_decision_cache_version("gemini-decision-1")
    assert "panel_002" not in saved_cache

    # Resuming makes ONLY 1 API call for the failed panel_002
    resume_calls = 0

    def _resume_handler(_: httpx.Request) -> httpx.Response:
        nonlocal resume_calls
        resume_calls += 1
        return httpx.Response(
            200,
            json=_four_sided_payload(
                panel_group="group_3_skip",
                edges=(0, 10, 0, 10),
            ),
        )

    ok_client = DecisionClient(
        max_retries=1,
        http_client=httpx.Client(transport=httpx.MockTransport(_resume_handler)),
    )
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=ok_client)
    assert resume_calls == 1
    assert summary["policy"] == "4sided"
    assert summary["counts_by_group"] == {"active": 1, "context_only": 0, "skip": 1}

    updated = load_manifest(chap_dir)
    assert updated is not None
    p1 = next(p for p in updated.panels if p.panel_id == "panel_001")
    assert p1.status == PanelStatus.ACTIVE
    assert p1.evidence_metadata["crop_method"] == "4sided_pct"
    assert p1.evidence_metadata["crop_edges_pct"] == {"left": 10, "top": 0, "right": 10, "bottom": 0}
    assert p1.evidence_metadata["display_crop"] == crop_edges_to_region(10, 0, 10, 0)

    # Immediate re-run uses cache with 0 API calls
    zero_calls = 0

    def _never_called(_: httpx.Request) -> httpx.Response:
        nonlocal zero_calls
        zero_calls += 1
        return httpx.Response(200, json=_four_sided_payload())

    cached_client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_never_called)))
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=cached_client)
    assert zero_calls == 0

    # Changing cache_version in cached file triggers recompute
    cache_obj = read_json(chap_dir / "decision_cache.json")
    for entry in cache_obj.values():
        entry["cache_version"] = "stale-version"
    atomic_write_json(chap_dir / "decision_cache.json", cache_obj)

    recompute_calls = 0

    def _recompute_handler(_: httpx.Request) -> httpx.Response:
        nonlocal recompute_calls
        recompute_calls += 1
        return httpx.Response(200, json=_four_sided_payload())

    recompute_client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_recompute_handler)))
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=recompute_client)
    assert recompute_calls >= 1


def test_pipeline_split_merge_and_compat_facade(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 1080, "height": 3200, "draw_gutter": True},
            {"panel_id": "panel_002", "width": 1080, "height": 1200},
            {"panel_id": "panel_003", "width": 1080, "height": 300},
        ],
    )

    call_log: List[Dict[str, Any]] = []

    def _split_merge_handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content.decode("utf-8"))
        q_keys = set(body["questions"].keys())
        ctx = next(item["text"] for item in body["input"] if item["type"] == "text")
        img_res = next(item.get("resolution") for item in body["input"] if item["type"] == "image")
        call_log.append({"q_keys": sorted(q_keys), "ctx": ctx, "resolution": img_res})

        if "primary_cut" in q_keys:
            return httpx.Response(200, json={"answers": {"primary_cut": {"choice": "cut_1", "confidence": 0.9}}, "usage": {"total_input_tokens": 50}})
        if "merge_decision" in q_keys:
            return httpx.Response(200, json={"answers": {"merge_decision": {"choice": "merge", "confidence": 0.91}}, "usage": {"total_input_tokens": 40}})
        if "1080x3200px" in ctx:
            return httpx.Response(
                200,
                json=_four_sided_payload(structure_action="needs_split", edges=(0, 10, 0, 0)),
            )
        if "1080x300px" in ctx:
            return httpx.Response(
                200,
                json=_four_sided_payload(structure_action="merge_with_prev", edges=(0, 0, 0, 0)),
            )
        return httpx.Response(200, json=_four_sided_payload(edges=(0, 0, 10, 0)))

    client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_split_merge_handler)))
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client)

    assert summary["policy"] == "4sided"
    assert summary["split_resolved"] == 1
    assert summary["pass2_reclassified"] == 3  # 2 split children + 1 merged child
    assert summary["final_total_panels"] == 3
    # Verify adaptive resolution: high on tall strip (1080x3200), medium on regular panel (1080x1200)
    pass1_res = {entry["ctx"]: entry["resolution"] for entry in call_log if "panel_group" in entry["q_keys"]}
    assert any("1080x3200px" in k and v == "high" for k, v in pass1_res.items())
    assert any("1080x1200px" in k and v == "medium" for k, v in pass1_res.items())
    final_manifest = load_manifest(chap_dir)
    assert final_manifest is not None
    assert all(p.evidence_metadata.get("crop_method") == "4sided_pct" for p in final_manifest.panels)
    assert all(p.evidence_metadata.get("display_crop") == crop_edges_to_region(0, 0, 10, 0) for p in final_manifest.panels)

    # Compatibility facade smoke check
    assert callable(compat_call_gemini_decision)
    assert callable(compat_run_decision_pipeline)
    img_path = chap_dir / "panels" / final_manifest.panels[0].filename
    dec = decide_single_panel(img_path, 400, 400, "key-123", client=client)
    assert dec["display_crop"] == crop_edges_to_region(0, 0, 10, 0)


def test_stale_legacy_metadata_conservative_fallback_and_cache_upgrade(tmp_path, monkeypatch):
    from src.core.visual_contract import Region
    from src.render.visual_policy import compute_adaptive_display_box, resolve_accepted_display_box

    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [{"panel_id": "panel_001", "width": 800, "height": 1200}],
    )
    with pytest.raises(ValueError, match="Only '4sided' is supported"):
        DecisionPolicyConfig(policy="legacy_9preset")

    # Old legacy_9preset manifest evidence must fail closed instead of guessing full frame crop
    man_legacy = load_manifest(chap_dir)
    man_legacy.panels[0].evidence_metadata = {
        "decision_policy": "legacy_9preset",
        "crop_method": "legacy_9preset",
        "center_crop_preset": "left_focus_2d",
        "display_crop": {"x_min": 0.0, "y_min": 0.08, "x_max": 0.82, "y_max": 0.92},
        "decision_valid": True,
        "crop_safe": True,
    }
    atomic_write_json(chap_dir / "manifest.json", man_legacy)
    atomic_write_json(
        chap_dir / "decision_cache.json",
        {
            "panel_001": {
                "sha256": man_legacy.panels[0].sha256,
                "width": 1080,
                "height": 1200,
                "cache_version": "legacy-v5",
                "raw_answers": {"panel_group": {"choice": "group_1_story_art"}},
                "decision": {"status": "active"},
            }
        },
    )
    with pytest.raises(ValueError, match="Incompatible legacy 9-preset crop metadata"):
        resolve_accepted_display_box(1080, 1200, man_legacy.panels[0].evidence_metadata)
    with pytest.raises(ValueError, match="Incompatible legacy 9-preset crop metadata"):
        compute_adaptive_display_box(1080, 1200, man_legacy.panels[0].evidence_metadata)

    # Running Decision pipeline invalidates legacy cache cleanly and upgrades metadata to canonical 4sided
    upgrade_calls = 0

    def _upgrade_handler(_: httpx.Request) -> httpx.Response:
        nonlocal upgrade_calls
        upgrade_calls += 1
        return httpx.Response(200, json=_four_sided_payload(edges=(10, 0, 0, 0)))

    default_client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_upgrade_handler)))
    res_upgraded = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=default_client)
    assert upgrade_calls == 1
    assert res_upgraded["policy"] == "4sided"
    man_upgraded = load_manifest(chap_dir)
    assert man_upgraded.panels[0].evidence_metadata["crop_method"] == "4sided_pct"
    assert "center_crop_preset" not in man_upgraded.panels[0].evidence_metadata
    assert man_upgraded.panels[0].evidence_metadata["display_crop"] == crop_edges_to_region(10, 0, 0, 0)
