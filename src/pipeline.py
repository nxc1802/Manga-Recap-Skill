"""Legacy compatibility surface; production work uses canonical CLI policy."""
from src.core.logger import logger
from src.core.utils import get_project_data_dir, cleanup_intermediate_files

class MangaRecapPipeline:
    def __init__(self):
        pass

    def cleanup_temp_files(self, chapter: int | str, suffix: str = "", project: str = None, keep_temp: bool = False) -> list:
        """
        Cleans up temporary video and subtitle files generated during chapter processing.
        """
        if not project:
            return []
        proj_name = project
        project_dir = get_project_data_dir(proj_name)
        chap_dir = project_dir / "chapters" / str(chapter)
        cleaned = []
        if chap_dir.exists():
            cleaned.extend(cleanup_intermediate_files(chap_dir, keep_temp=keep_temp))
        video_chap_dir = project_dir / "video" / "chapters"
        if video_chap_dir.exists():
            cleaned.extend(cleanup_intermediate_files(video_chap_dir, keep_temp=keep_temp))
        logger.info(f"Cleaned up {len(cleaned)} temporary files for chapter {chapter}: {cleaned}")
        return cleaned

    async def run(self, *args, **kwargs):
        from src.cli.exit_codes import CliValidationError
        raise CliValidationError("Legacy pipeline.run is disabled. Use manga-recap crawl/tts/render with explicit project setup.")

    async def _process_single_panel_clip(self, *args, **kwargs):
        from src.cli.exit_codes import CliValidationError
        raise CliValidationError("Legacy clip orchestration is disabled; render a canonical VisualPlan")

    async def generate_video(self, chapter_id, chapter_number, bgm_path=None,
                             output_suffix=None, progress_callback=None,
                             burn_subtitles=None, video_mode=None):
        import argparse
        from src.core.execution_policy import resolve_legacy_chapter
        from src.cli.render_cmd import handle_render
        project = resolve_legacy_chapter(chapter_id, chapter_number)
        result = handle_render(argparse.Namespace(project=project.name, chapter=str(chapter_number),
            quality="final", bgm=bgm_path, burn_subtitles=burn_subtitles, video_mode=video_mode))
        return result["video_path"]


pipeline = MangaRecapPipeline()
