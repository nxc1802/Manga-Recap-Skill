"""Render a chapter and stage its preview review pack or final exports."""
import argparse
from src.cli.exit_codes import CliValidationError
from src.core.storage import get_project_dir, stage_chapter_export
from src.core.execution_policy import enforce_execution_policy
from src.render.chapter_renderer import render_chapter
from src.core.utils import assert_file_nonempty


def handle_render(args: argparse.Namespace):
    enforce_execution_policy("render", args)
    slug, chapter_num = args.project, str(args.chapter)
    if getattr(args, "allow_placeholders", False):
        raise CliValidationError("Placeholder artifacts cannot enter the production pipeline")
    encoder = getattr(args, "encoder", None)
    hw = getattr(args, "hw_accel", None)
    if encoder is None and hw:
        encoder = {"none": "libx264", "auto": "auto", "videotoolbox": "h264_videotoolbox"}[hw]
    workers = getattr(args, "workers", None) or 4
    options = dict(quality=getattr(args, "quality", "final"), encoder=encoder, render_workers=workers,
        bgm_path=getattr(args, "bgm", None), burn_subtitles=getattr(args, "burn_subtitles", None),
        allow_animation=getattr(args, "animation", None), video_mode=getattr(args, "video_mode", None),
        subtitle_lines=getattr(args, "subtitle_lines", None), subtitle_font_size=getattr(args, "subtitle_font_size", None),
        subtitle_chunking=getattr(args, "subtitle_chunking", None))
    if getattr(args, "kaggle", False):
        expected_quality = "preview" if chapter_num == "1" else "final"
        if options["quality"] != expected_quality:
            raise CliValidationError("Cloud renders chapter 1 preview and later chapters final. Promote reviewed chapter 1 locally.")
        from src.cloud.kaggle_service import KaggleCloudService
        result = KaggleCloudService().dispatch_chapter_job_with_fallback(slug, chapter_num, job_type="render", wait=True,
            encoder_preference=options["encoder"] or "auto", burn_subtitles=options["burn_subtitles"],
            render_workers=workers, video_mode=options["video_mode"])
    else:
        result = render_chapter(get_project_dir(slug), chapter_num, **options)
    assert_file_nonempty(result["video_path"])
    assert_file_nonempty(result["subtitle_path"])
    if options["quality"] == "final":
        if "export_paths" not in result:
            staged = stage_chapter_export(slug, chapter_num, result["video_path"], result["subtitle_path"])
            result["export_paths"] = {key: str(value) if value else None for key, value in staged.items()}
        if getattr(args, "cleanup", False):
            from src.cli.clean_cmd import handle_clean

            result["cleanup"] = handle_clean(argparse.Namespace(
                project=slug, chapter=chapter_num, dry_run=False, confirm_project=slug,
            ))
    else:
        from src.cli.review_pack import handle_review_pack
        result["review_pack"] = handle_review_pack(argparse.Namespace(project=slug, chapter=chapter_num, format="all", out_dir=None))
    return result
