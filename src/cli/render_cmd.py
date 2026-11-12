"""Render CLI: canonical renderer, semantic validation, then checkpoint/export."""
import argparse
from src.cli.exit_codes import CliValidationError
from src.core.storage import (get_project_dir, get_chapter_dir, load_chapter_metadata,
                              atomic_write_json, stage_chapter_export, acquire_lock)
from src.core.schemas import CheckpointStatus, HumanReviewStatus
from src.core.execution_policy import enforce_execution_policy
from src.render.video_engine import VideoEngine


def handle_render(args: argparse.Namespace):
    enforce_execution_policy("render", args)
    slug, chapter_num = args.project, str(args.chapter)
    if getattr(args, "allow_placeholders", False):
        raise CliValidationError("Placeholder artifacts cannot enter the production pipeline")
    options = dict(quality=getattr(args, "quality", "final"), encoder=getattr(args, "encoder", None),
        bgm_path=getattr(args, "bgm", None), burn_subtitles=getattr(args, "burn_subtitles", None),
        allow_animation=getattr(args, "animation", None), video_mode=getattr(args, "video_mode", None),
        subtitle_lines=getattr(args, "subtitle_lines", None), subtitle_font_size=getattr(args, "subtitle_font_size", None),
        subtitle_chunking=getattr(args, "subtitle_chunking", None))
    if getattr(args, "kaggle", False):
        expected_quality = "preview" if chapter_num == "1" else "final"
        if options["quality"] != expected_quality:
            raise CliValidationError("Cloud renders chapter 1 preview and later chapters final. Promote reviewed chapter 1 locally.")
        from src.cloud.kaggle_service import KaggleCloudService
        result = KaggleCloudService().dispatch_chapter_job(slug, chapter_num, job_type="render", wait=True,
            encoder_preference=options["encoder"] or "auto", burn_subtitles=options["burn_subtitles"])
    else:
        result = VideoEngine().render_chapter(get_project_dir(slug), chapter_num, **options)
    # Do not trust a renderer's success flag. Validate actual artifacts before
    # granting completed/export, including when a backend returns stale paths.
    from src.render.artifact_validation import validate_final_artifact
    from src.core.storage import load_manifest, load_recap_script
    from src.core.visual_contract import VisualPlan
    from src.render.chapter_renderer import resolve_audio
    chapter = get_chapter_dir(slug, chapter_num)
    script = load_recap_script(chapter)
    report = validate_final_artifact(result["video_path"], result["subtitle_path"],
        [resolve_audio(chapter, seg) for seg in script.segments], VisualPlan.load(chapter / "visual_plan.json"),
        script, load_manifest(chapter), chapter_num, chapter_dir=chapter)
    atomic_write_json(chapter / "artifact_validation.json", report)
    if options["quality"] == "final":
        project_dir = get_project_dir(slug)
        reviewed_status = HumanReviewStatus.UNREVIEWED
        if chapter_num == "1":
            from src.core.human_review import require_preview_approval
            approval = require_preview_approval(project_dir)
            if report["video"]["sha256"] != approval.artifact_hash:
                raise CliValidationError("Chapter 1 final artifact differs from human-reviewed preview")
            reviewed_status = HumanReviewStatus.APPROVED
        with acquire_lock(chapter / "chapter.json"):
            metadata = load_chapter_metadata(chapter)
            metadata.overall_status = metadata.technical_status = CheckpointStatus.COMPLETED
            metadata.human_review_status = reviewed_status
            metadata.stats.video_path = result["video_path"]
            metadata.stats.final_video_duration_seconds = report["video"]["duration"]
            atomic_write_json(chapter / "chapter.json", metadata)
        if (project_dir / "release_state.json").is_file():
            from src.core.contracts import ReleaseState
            from src.core.state_machine import advance_release_state, load_release_record
            rec = load_release_record(project_dir)
            if rec.chapter_number == chapter_num and rec.state == ReleaseState.HUMAN_APPROVED:
                advance_release_state(project_dir, ReleaseState.PRODUCTION_READY, chapter_number=chapter_num)
        staged = stage_chapter_export(slug, chapter_num, result["video_path"], result["subtitle_path"])
        result["export_paths"] = {key: str(value) if value else None for key, value in staged.items()}
    else:
        from src.cli.review_pack import handle_review_pack
        result["review_pack"] = handle_review_pack(argparse.Namespace(project=slug, chapter=chapter_num, format="all", out_dir=None))
    return result
