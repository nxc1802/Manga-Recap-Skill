"""
src/cli/main.py
Unified CLI entry point for Manga Recap Agentic Skill V3 (`manga-recap`).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.response import print_response
from src.core.logger import configure_logging
from src.core.storage import (
    InvalidCheckpointTransitionError,
    StorageCorruptError,
    StorageError,
    StorageFileNotFoundError,
    StorageLockError,
    StorageWriteError,
)
from src.core.utils import DependencyNotFoundError


class JsonAwareArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser that intercepts parse errors and raises CliValidationError
    when --json flag is supplied, or delegates to standard argparse.error otherwise.
    Propagates json_mode recursively to all child subparsers.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._json_mode = False
        self.start_time = time.monotonic()
        self.detected_subcommand = "manga-recap"

    @property
    def json_mode(self) -> bool:
        return self._json_mode

    @json_mode.setter
    def json_mode(self, value: bool) -> None:
        self._json_mode = value
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice_parser in action.choices.values():
                    if isinstance(choice_parser, JsonAwareArgumentParser):
                        choice_parser.json_mode = value

    def error(self, message: str) -> None:
        if self._json_mode:
            raise CliValidationError(message)
        super().error(message)


def build_parser() -> JsonAwareArgumentParser:
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--json", action="store_true", help="Output JSON envelope to stdout")
    common_parent.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs on stderr")

    parser = JsonAwareArgumentParser(
        prog="manga-recap",
        description="Deterministic CLI Suite for Manga Recap Agentic Skill V3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[common_parent],
    )
    parser.add_argument("--version", action="version", version="%(prog)s 3.0.0")

    subparsers = parser.add_subparsers(dest="subcommand", title="subcommands", required=True)

    # 1. inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect project or chapter assets and metrics", parents=[common_parent])
    p_inspect.add_argument("-p", "--project", required=True, help="Project slug")
    p_inspect.add_argument("-c", "--chapter", default=None, help="Chapter number (optional)")

    # 2. validate
    p_val = subparsers.add_parser("validate", help="Validate schema conformance and file presence", parents=[common_parent])
    p_val.add_argument("-p", "--project", required=True, help="Project slug")
    p_val.add_argument("-c", "--chapter", default=None, help="Chapter number (optional)")
    p_val.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    p_val.add_argument("--pacing", action="store_true", help="Generate pre-TTS script pacing diagnostic report")

    # 3. status
    p_stat = subparsers.add_parser("status", help="Get chapter checkpoints and resumable stage", parents=[common_parent])
    p_stat.add_argument("-p", "--project", required=True, help="Project slug")
    p_stat.add_argument("-c", "--chapter", default=None, help="Chapter number (optional)")

    # 4. crawl
    p_crawl = subparsers.add_parser("crawl", help="Crawl or import chapter manga images", parents=[common_parent])
    p_crawl.add_argument("-p", "--project", required=True, help="Project slug")
    p_crawl.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_crawl.add_argument("-u", "--url", default=None, help="Source URL to scrape")
    p_crawl.add_argument("--import-dir", default=None, help="Local directory to import raw images from")
    p_crawl.add_argument("--max-panels", type=int, default=None, help="Max images limit")
    p_crawl.add_argument("--verify", action="store_true", help="Perform strict image integrity and continuity verification")
    p_crawl.add_argument("--profile", default=None, help="Specific source profile domain override")


    # 4b. segment
    p_seg = subparsers.add_parser("segment", help="Segment raw manga chapter pages into panels and contact sheets", parents=[common_parent])
    p_seg.add_argument("-p", "--project", required=True, help="Project slug")
    p_seg.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_seg.add_argument("--target-width", type=int, default=1080, help="Target panel width")
    p_seg.add_argument("--force", action="store_true", help="Force re-segmentation")

    # 5. tts
    p_tts = subparsers.add_parser("tts", help="Synthesize audio for script segments", parents=[common_parent])
    p_tts.add_argument("-p", "--project", required=True, help="Project slug")
    p_tts.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_tts.add_argument("--voice", default=None, help="Preset voice override")
    p_tts.add_argument("--speed", type=float, default=None, help="Speech speed override")
    p_tts.add_argument("--force", action="store_true", help="Re-synthesize all audio segments even if existing")
    p_tts.add_argument("--no-cache", action="store_true", help="Bypass and delete any existing cached audio clips and re-synthesize cleanly")
    p_tts.add_argument("--engine", choices=["neural", "mock", "auto"], default="neural", help="TTS synthesis engine backend (default: neural)")
    p_tts.add_argument("--mock", action="store_true", help="Shortcut for --engine mock (testing only)")
    p_tts.add_argument("--workers", type=int, default=None, help="Number of concurrent worker threads (default: 2 for Balanced mode)")
    p_tts.add_argument("--kaggle", action="store_true", help="Offload TTS speech synthesis to Kaggle Cloud GPU")

    # 6. tts_benchmark
    p_bm = subparsers.add_parser("tts_benchmark", help="Run TTS voice and emotion screening benchmark", parents=[common_parent])
    p_bm.add_argument("-o", "--out-dir", default="data/benchmarks/vieneu_v3_turbo", help="Output directory")
    p_bm.add_argument("--voices", default="all", help="Comma-separated voice IDs, 'top10', or 'all'")
    p_bm.add_argument("--emotions", default="all", help="Comma-separated emotion IDs or 'all'")
    p_bm.add_argument("--repeats", type=int, default=1, help="Number of repetitions per prompt")
    p_bm.add_argument("--mock", action="store_true", help="Use deterministic mock synthesis mode")
    p_bm.add_argument("--ab-tests", action="store_true", help="Include controlled A/B emotion cue comparison tests")
    p_bm.add_argument("--copy-to-lab", action=argparse.BooleanOptionalAction, default=True, help="Copy results to docs/lab/")

    # 7. render
    p_ren = subparsers.add_parser("render", help="Render video clips, mix audio, and burn subtitles", parents=[common_parent])
    p_ren.add_argument("-p", "--project", required=True, help="Project slug")
    p_ren.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_ren.add_argument("--quality", choices=["preview", "final"], default="final", help="Render quality")
    p_ren.add_argument("--bgm", default=None, help="BGM audio file path")
    p_ren.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default=None, help="Video mode")
    p_ren.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None, help="Burn subtitles")
    p_ren.add_argument("--animation", action=argparse.BooleanOptionalAction, default=None, help="Enable cinematic camera motion animations")
    p_ren.add_argument("--suffix", default="final", help="Output version suffix")
    p_ren.add_argument("--force", action="store_true", help="Ignore existing panel clips and force re-render")
    p_ren.add_argument("--keep-temp", action=argparse.BooleanOptionalAction, default=None, help="Preserve intermediate clip files (default: True unless --cleanup)")
    p_ren.add_argument("--skip-review", action="store_true", help="Skip human review gate verification (deprecated for final render)")
    p_ren.add_argument("--skip-preview", action="store_true", help="Skip Review Gate 2 preview requirement and render final 1080p directly")
    p_ren.add_argument("--approved", action="store_true", help="Explicitly approve Review Gate 1 (Voice) and Gate 2 (Preview) for automated/programmatic runs")
    p_ren.add_argument("--allow-placeholders", action="store_true", help="Allow dummy placeholder images/silence if assets are missing (testing only)")
    p_ren.add_argument("--cleanup", action="store_true", help="Safely clean up temporary intermediate clips after verified final render")
    p_ren.add_argument("--workers", type=int, default=None, help="Number of concurrent worker threads (default: 2 for Balanced mode)")
    p_ren.add_argument("--hw-accel", choices=["none", "videotoolbox", "auto"], default=None, help="Hardware acceleration encoder")
    p_ren.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default=None, help="Video encoder (default: auto or matches --hw-accel)")
    p_ren.add_argument("--subtitle-lines", type=int, default=None, choices=[1, 2], help="Maximum subtitle lines per cue (default: 1)")
    p_ren.add_argument("--subtitle-font-size", type=int, default=None, help="Subtitle base font size at 1280px (default: 26)")
    p_ren.add_argument("--subtitle-chunking", choices=["sentence-aware", "fixed-chars"], default=None, help="Subtitle chunking strategy (default: sentence-aware)")
    p_ren.add_argument("--kaggle", action="store_true", help="Offload video rendering and TTS to Kaggle Cloud GPU")

    # 8. compile
    p_com = subparsers.add_parser("compile", help="Concatenate chapter videos into final recap", parents=[common_parent])
    p_com.add_argument("-p", "--project", required=True, help="Project slug")
    p_com.add_argument("--chapters", default=None, help="Comma-separated chapter numbers")
    p_com.add_argument("-o", "--output", default=None, help="Output final video path")
    p_com.add_argument("--approved", action="store_true", help="Explicitly approve Review Gate 1 (Voice) and Gate 2 (Preview) for automated/programmatic runs")
    p_com.add_argument("--extract-audio", action=argparse.BooleanOptionalAction, default=True, help="Extract master WAV")

    # 9. batch
    p_bat = subparsers.add_parser("batch", help="Sequential multi-chapter batch processing", parents=[common_parent])
    p_bat.add_argument("-p", "--project", required=True, help="Project slug")
    p_bat.add_argument("-c", "--chapters", default=None, help="Comma-separated or range list of chapters (e.g. '1,2,3' or '1-5')")
    p_bat.add_argument("--retry-chapter", default=None, help="Specific chapter number to retry")
    p_bat.add_argument("--skip-preview", action=argparse.BooleanOptionalAction, default=True, help="Skip preview video and render final 1080p directly (default: True)")
    p_bat.add_argument("--compile", action="store_true", help="Auto-compile completed chapters into master recap video")
    p_bat.add_argument("--force", action="store_true", help="Force re-processing of already completed chapters")
    p_bat.add_argument("--approved", action="store_true", help="Explicitly approve Review Gates for automated runs")
    p_bat.add_argument("--allow-placeholders", action="store_true", help="Allow dummy placeholder assets (testing only)")
    p_bat.add_argument("--voice", default=None, help="Voice name override")
    p_bat.add_argument("--speed", type=float, default=None, help="TTS speed override")
    p_bat.add_argument("--bgm", default=None, help="BGM audio file path")
    p_bat.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default="auto", help="Video mode")
    p_bat.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=True, help="Burn subtitles")
    p_bat.add_argument("--animation", action=argparse.BooleanOptionalAction, default=True, help="Enable cinematic camera motion animations")
    p_bat.add_argument("--subtitle-lines", type=int, default=None, choices=[1, 2], help="Maximum subtitle lines per cue")
    p_bat.add_argument("--subtitle-font-size", type=int, default=None, help="Subtitle base font size at 1280px")
    p_bat.add_argument("--subtitle-chunking", choices=["sentence-aware", "fixed-chars"], default=None, help="Subtitle chunking strategy")
    p_bat.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default=None, help="Video encoder for rendering (default: auto)")
    p_bat.add_argument("--tts-engine", choices=["neural", "mock"], default="neural", help="TTS engine mode (default: neural)")
    p_bat.add_argument("--tts-workers", type=int, default=2, help="TTS synthesis concurrent workers")
    p_bat.add_argument("--render-workers", type=int, default=2, help="Video render concurrent workers")
    p_bat.add_argument("--keep-temp", action=argparse.BooleanOptionalAction, default=None, help="Preserve intermediate clip files (default: True unless --cleanup)")
    p_bat.add_argument("--cleanup", action="store_true", help="Safely clean up temporary intermediate clips after verified final render")
    p_bat.add_argument("--kaggle", action="store_true", help="Offload batch processing (TTS + Rendering) to Kaggle Cloud GPU")

    # 10. youtube
    p_yt = subparsers.add_parser("youtube", help="YouTube metadata packaging, SEO, and video uploading", parents=[common_parent])
    p_yt.add_argument("youtube_action", choices=["metadata", "prepare", "upload", "auth", "thumbnail", "accounts"], help="YouTube action: 'metadata', 'upload', 'auth', 'thumbnail', or 'accounts'")
    p_yt.add_argument("-p", "--project", default=None, help="Project slug")
    p_yt.add_argument("-c", "--chapter", default="1", help="Chapter number (or 'feature')")
    p_yt.add_argument("--account", default=None, help="Target YouTube account/channel name (default: active default account)")
    p_yt.add_argument("--account-action", choices=["list", "add", "remove", "default"], default="list", help="Account management action (default: list)")
    p_yt.add_argument("--secrets", default=None, help="Path to client_secrets.json")
    p_yt.add_argument("--set-default", action="store_true", help="Set this account as the default account")
    p_yt.add_argument("--delete-token", action=argparse.BooleanOptionalAction, default=True, help="Delete stored token file on account removal")
    p_yt.add_argument("--force", action="store_true", help="Force re-authentication")
    p_yt.add_argument("--video", default=None, help="Custom video file path override")
    p_yt.add_argument("--subtitles", default=None, help="Custom subtitles SRT file path override")
    p_yt.add_argument("--title", default=None, help="Video title override")
    p_yt.add_argument("--description", default=None, help="Video description override")
    p_yt.add_argument("--tags", default=None, help="Comma-separated tags")
    p_yt.add_argument("--privacy", choices=["public", "private", "unlisted"], default="private", help="Video privacy status (default: private)")
    p_yt.add_argument("--thumbnail", default=None, help="Custom thumbnail image path")
    p_yt.add_argument("--select", default=None, help="Candidate ID to select for thumbnail (e.g. cand_01)")
    p_yt.add_argument("--no-captions", action="store_true", help="Skip uploading Vietnamese closed captions")

    # 11. thumbnail (top-level shortcut)
    p_thumb = subparsers.add_parser("thumbnail", help="Generate or select YouTube thumbnail candidates", parents=[common_parent])
    p_thumb.add_argument("-p", "--project", required=True, help="Project slug")
    p_thumb.add_argument("-c", "--chapter", default="1", help="Chapter number (or 'feature')")
    p_thumb.add_argument("--select", default=None, help="Candidate ID to select (e.g. cand_01)")

    # 12. clean
    p_cln = subparsers.add_parser("clean", help="Safely clean intermediate caches and temporary clips", parents=[common_parent])
    p_cln.add_argument("-p", "--project", required=True, help="Project slug")
    p_cln.add_argument("-c", "--chapter", default=None, help="Specific chapter number (default: all completed chapters)")
    p_cln.add_argument("--dry-run", action="store_true", help="Report reclaimable storage without unlinking files")

    # 13. produce
    p_prod = subparsers.add_parser("produce", help="Single-session cloud production runner (Kaggle/Colab)", parents=[common_parent])
    p_prod.add_argument("-p", "--project", required=True, help="Project slug")
    p_prod.add_argument("-c", "--chapters", default=None, help="Comma-separated or range list of chapters (e.g. '1,2,3' or '1-5')")
    p_prod.add_argument("--job-file", default=None, help="Custom path to production_job.json")
    p_prod.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default="auto", help="Video encoder (default: auto)")
    p_prod.add_argument("--tts-engine", choices=["neural", "mock"], default="neural", help="TTS engine mode (default: neural)")
    p_prod.add_argument("--tts-workers", type=int, default=2, help="TTS synthesis concurrent workers (default: 2)")
    p_prod.add_argument("--render-workers", type=int, default=2, help="Video clip render concurrent workers (default: 2)")
    p_prod.add_argument("--quality", choices=["preview", "final"], default="final", help="Render quality (default: final)")
    p_prod.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default="auto", help="Video framing mode")
    p_prod.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=True, help="Burn subtitles directly onto video frames")
    p_prod.add_argument("--animation", action=argparse.BooleanOptionalAction, default=True, help="Enable cinematic camera motion animations")
    p_prod.add_argument("--bgm", default=None, help="Background music audio file path")
    p_prod.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True, help="Compile feature video across chapters (default: True)")
    p_prod.add_argument("--export-bundle", action=argparse.BooleanOptionalAction, default=True, help="Stage complete export bundle (default: True)")
    p_prod.add_argument("--cleanup", action="store_true", help="Clean intermediate clips after successful render")
    p_prod.add_argument("--force", action="store_true", help="Force re-execution of completed stages")
    p_prod.add_argument("--voice", default=None, help="Voice override")
    p_prod.add_argument("--speed", type=float, default=None, help="Speed override")
    p_prod.add_argument("--subtitle-lines", type=int, default=1, choices=[1, 2], help="Subtitle max lines")
    p_prod.add_argument("--subtitle-font-size", type=int, default=26, help="Subtitle font size")
    p_prod.add_argument("--subtitle-chunking", choices=["sentence-aware", "fixed-chars"], default="sentence-aware", help="Subtitle chunking strategy")

    # 14. cloud
    p_cld = subparsers.add_parser("cloud", help="Kaggle/Remote Cloud GPU runner for extreme speed", parents=[common_parent])
    p_cld.add_argument("cloud_action", choices=["run", "info", "pull"], default="run", nargs="?", help="Action (run, info, pull)")
    p_cld.add_argument("-p", "--project", default=None, help="Project slug")
    p_cld.add_argument("-c", "--chapter", default=None, help="Chapter number")
    p_cld.add_argument("--provider", choices=["kaggle"], default="kaggle", help="Cloud provider (default: kaggle)")
    p_cld.add_argument("--no-gpu", action="store_true", help="Disable GPU and run CPU mode")
    p_cld.add_argument("--extreme", action="store_true", default=True, help="Enable extreme speed optimizations")

    # 15. review-pack
    p_rp = subparsers.add_parser("review-pack", aliases=["review_pack"], help="Generate lightweight static human review pack", parents=[common_parent])
    p_rp.add_argument("-p", "--project", required=True, help="Project slug")
    p_rp.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_rp.add_argument("--format", choices=["md", "html", "json", "all"], default="all", help="Output format(s)")
    p_rp.add_argument("--out-dir", default=None, help="Custom output directory")

    return parser


def execute_subcommand(subcommand: str, args: argparse.Namespace) -> Dict[str, Any]:
    """Dispatches parsed arguments to command implementations."""
    if subcommand == "inspect":
        from src.cli.inspect_cmd import handle_inspect
        return handle_inspect(args)
    elif subcommand == "validate":
        from src.cli.validate_cmd import handle_validate
        return handle_validate(args)
    elif subcommand == "status":
        from src.cli.status_cmd import handle_status
        return handle_status(args)
    elif subcommand == "crawl":
        from src.cli.crawl_cmd import handle_crawl
        return handle_crawl(args)
    elif subcommand == "segment":
        from src.cli.segment_cmd import handle_segment
        return handle_segment(args)
    elif subcommand == "tts":
        from src.cli.tts_cmd import handle_tts
        return handle_tts(args)
    elif subcommand == "tts_benchmark":
        from src.cli.tts_benchmark_cmd import handle_tts_benchmark
        return handle_tts_benchmark(args)
    elif subcommand == "render":
        from src.cli.render_cmd import handle_render
        return handle_render(args)
    elif subcommand == "compile":
        from src.cli.compile_cmd import handle_compile
        return handle_compile(args)
    elif subcommand == "batch":
        from src.cli.batch_cmd import handle_batch
        return handle_batch(args)
    elif subcommand == "youtube":
        from src.cli.youtube_cmd import handle_youtube
        return handle_youtube(args)
    elif subcommand == "thumbnail":
        from src.cli.youtube_cmd import handle_youtube_thumbnail
        return handle_youtube_thumbnail(args)
    elif subcommand == "clean":
        from src.cli.clean_cmd import handle_clean
        return handle_clean(args)
    elif subcommand == "produce":
        from src.cli.production_cmd import handle_produce
        return handle_produce(args)
    elif subcommand == "cloud":
        from src.cli.cloud_cmd import handle_cloud
        return handle_cloud(args)
    elif subcommand in {"review-pack", "review_pack"}:
        from src.cli.review_pack import handle_review_pack
        return handle_review_pack(args)
    else:
        raise ValueError(f"Unknown subcommand: {subcommand}")



def main(argv: Optional[List[str]] = None) -> int:
    """CLI execution entrypoint."""
    start_time = time.monotonic()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    json_mode = "--json" in raw_args
    verbose = "-v" in raw_args or "--verbose" in raw_args

    # Configure logger to output exclusively to stderr
    configure_logging(verbose=verbose)

    # Detect subcommand early for error reporting
    detected_subcommand = "manga-recap"
    for arg in raw_args:
        if not arg.startswith("-") and arg in {
            "crawl", "segment", "inspect", "validate", "tts", "tts_benchmark", "render", "compile", "status", "batch", "youtube", "thumbnail", "clean", "produce", "cloud", "review-pack", "review_pack"
        }:
            detected_subcommand = arg
            break

    parser = build_parser()
    parser.json_mode = json_mode
    parser.start_time = start_time
    parser.detected_subcommand = detected_subcommand

    # Propagate json_mode and timing to all subparsers so subparser errors also raise CliValidationError
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub_name, subparser in action.choices.items():
                if isinstance(subparser, JsonAwareArgumentParser):
                    subparser.json_mode = json_mode
                    subparser.start_time = start_time
                    subparser.detected_subcommand = sub_name

    subcommand = detected_subcommand

    try:
        args = parser.parse_args(raw_args)
        subcommand = getattr(args, "subcommand", detected_subcommand)
        data = execute_subcommand(subcommand, args)
        duration = round(time.monotonic() - start_time, 4)
        print_response(
            command=subcommand,
            exit_code=ExitCode.SUCCESS,
            data=data,
            error=None,
            duration_seconds=duration,
            json_mode=json_mode,
        )
        return ExitCode.SUCCESS

    except SystemExit as se:
        code = se.code if isinstance(se.code, int) else 0
        return code

    except (CliValidationError, ValidationError, StorageCorruptError, InvalidCheckpointTransitionError) as e:
        duration = round(time.monotonic() - start_time, 4)
        error_data = getattr(e, "data", None)
        error_details = getattr(e, "details", None)
        error_detail = {
            "code": "VALIDATION_ERROR",
            "message": str(e),
            "details": error_details,
        }
        print_response(
            command=subcommand,
            exit_code=ExitCode.VALIDATION_ERROR,
            data=error_data,
            error=error_detail,
            duration_seconds=duration,
            json_mode=json_mode,
        )
        return ExitCode.VALIDATION_ERROR

    except (
        StorageFileNotFoundError,
        FileNotFoundError,
        PermissionError,
        StorageWriteError,
        StorageLockError,
        OSError,
    ) as e:
        duration = round(time.monotonic() - start_time, 4)
        error_detail = {"code": "IO_ERROR", "message": str(e)}
        print_response(
            command=subcommand,
            exit_code=ExitCode.IO_ERROR,
            data=None,
            error=error_detail,
            duration_seconds=duration,
            json_mode=json_mode,
        )
        return ExitCode.IO_ERROR

    except DependencyNotFoundError as e:
        duration = round(time.monotonic() - start_time, 4)
        error_detail = {"code": "DEPENDENCY_ERROR", "message": str(e)}
        print_response(
            command=subcommand,
            exit_code=ExitCode.DEPENDENCY_ERROR,
            data=None,
            error=error_detail,
            duration_seconds=duration,
            json_mode=json_mode,
        )
        return ExitCode.DEPENDENCY_ERROR

    except Exception as e:
        duration = round(time.monotonic() - start_time, 4)
        error_detail = {"code": "GENERAL_ERROR", "message": str(e)}
        print_response(
            command=subcommand,
            exit_code=ExitCode.GENERAL_ERROR,
            data=None,
            error=error_detail,
            duration_seconds=duration,
            json_mode=json_mode,
        )
        return ExitCode.GENERAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
