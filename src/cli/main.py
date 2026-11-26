"""
src/cli/main.py
Unified CLI entry point for Manga Recap V5 Slim (`manga-recap`).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.response import print_response
from src.core.errors import (
    DependencyNotFoundError,
    DomainValidationError,
    InvalidCheckpointTransitionError,
    StorageCorruptError,
    StorageError,
    StorageFileNotFoundError,
    StorageLockError,
    StorageWriteError,
)
from src.core.logger import configure_logging
from src.core.settings import load_runtime_dotenv
from src.core.version import APP_VERSION


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
        description="Deterministic CLI Suite for Manga Recap V5 Slim",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[common_parent],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")

    subparsers = parser.add_subparsers(dest="subcommand", title="subcommands", required=True)

    p_plan = subparsers.add_parser("visual-plan", help="Generate or regenerate canonical shots from source/audio evidence", parents=[common_parent])
    p_plan.add_argument("-p", "--project", required=True)
    p_plan.add_argument("-c", "--chapter", required=True)
    p_plan.add_argument("--regenerate", action="store_true")

    p_review = subparsers.add_parser("review-approve", help="Record explicit human approval of an exact preview", parents=[common_parent])
    p_review.add_argument("-p", "--project", required=True)
    p_review.add_argument("--reviewer", required=True)
    p_review.add_argument("--config-fingerprint", required=True)
    p_review.add_argument("--artifact-hash", required=True)

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

    # 4c. repack
    p_repack = subparsers.add_parser("repack", help="Apply AI VLM panel_plan.json (merge, split, 3-group classification)", parents=[common_parent])
    p_repack.add_argument("-p", "--project", required=True, help="Project slug")
    p_repack.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_repack.add_argument("--plan", default=None, help="Path to panel_plan.json (default: chapter/panel_plan.json)")
    p_repack.add_argument("--target-width", type=int, default=1080, help="Target panel width")
    p_repack.add_argument("--grid-cols", type=int, default=4, help="Contact sheet columns (default: 4)")
    p_repack.add_argument("--grid-rows", type=int, default=4, help="Contact sheet rows (default: 4)")

    # 4d. panel-inspect
    p_pinspect = subparsers.add_parser("panel-inspect", help="Render candidate cut lines for flagged panels", parents=[common_parent])
    p_pinspect.add_argument("-p", "--project", required=True, help="Project slug")
    p_pinspect.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_pinspect.add_argument("--panels", required=True, help="Comma-separated panel IDs to inspect")

    # 4e. decide
    p_decide = subparsers.add_parser("decide", help="Classify panels, resolve splits/merges, and assign 2D crops via gemini-decision-1", parents=[common_parent])
    p_decide.add_argument("-p", "--project", required=True, help="Project slug")
    p_decide.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_decide.add_argument("--workers", type=int, default=2, help="Concurrent Decision API requests (default: 2)")
    p_decide.add_argument("--reclassify-all", action="store_true", help="Re-evaluate all panels in Pass 2")

    # 5. tts
    p_tts = subparsers.add_parser("tts", help="Synthesize audio for script segments", parents=[common_parent])
    p_tts.add_argument("-p", "--project", required=True, help="Project slug")
    p_tts.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_tts.add_argument("--voice", default=None, help="Preset voice override")
    p_tts.add_argument("--speed", type=float, default=None, help="Speech speed override")
    p_tts.add_argument("--force", action="store_true", help="Re-synthesize all audio segments even if existing")
    p_tts.add_argument("--no-cache", action="store_true", help="Bypass and delete any existing cached audio clips and re-synthesize cleanly")
    p_tts.add_argument("--engine", choices=["neural"], default="neural", help="Production requires local neural VieNeu-TTS")
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
    p_ren.add_argument("--skip-review", action="store_true", help="Deprecated compatibility flag; never bypasses approval")
    p_ren.add_argument("--skip-preview", action="store_true", help="Compatibility flag; never bypasses Chapter 1 approval")
    p_ren.add_argument("--approved", action="store_true", help="Compatibility flag; approval must be recorded with review-approve")
    p_ren.add_argument("--allow-placeholders", action="store_true", help="Deprecated; production rejects placeholder artifacts")
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
    p_com.add_argument("--approved", action="store_true", help="Compatibility flag; approval must be recorded with review-approve")
    p_com.add_argument("--extract-audio", action=argparse.BooleanOptionalAction, default=True, help="Extract master WAV")

    # 9. batch
    p_bat = subparsers.add_parser("batch", help="Sequential multi-chapter batch processing", parents=[common_parent])
    p_bat.add_argument("-p", "--project", required=True, help="Project slug")
    p_bat.add_argument("-c", "--chapters", default=None, help="Comma-separated or range list of chapters (e.g. '1,2,3' or '1-5')")
    p_bat.add_argument("--retry-chapter", default=None, help="Specific chapter number to retry")
    p_bat.add_argument("--skip-preview", action=argparse.BooleanOptionalAction, default=True, help="Render final after exact Chapter 1 approval; cannot bypass G6")
    p_bat.add_argument("--compile", action="store_true", help="Auto-compile completed chapters into master recap video")
    p_bat.add_argument("--force", action="store_true", help="Force re-processing of already completed chapters")
    p_bat.add_argument("--approved", action="store_true", help="Explicitly approve Review Gates for automated runs")
    p_bat.add_argument("--allow-placeholders", action="store_true", help="Allow dummy placeholder assets (testing only)")
    p_bat.add_argument("--voice", default=None, help="Voice name override")
    p_bat.add_argument("--speed", type=float, default=None, help="TTS speed override")
    p_bat.add_argument("--bgm", default=None, help="BGM audio file path")
    p_bat.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default=None, help="Video mode")
    p_bat.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None, help="Burn subtitles")
    p_bat.add_argument("--animation", action=argparse.BooleanOptionalAction, default=None, help="Enable cinematic camera motion animations")
    p_bat.add_argument("--subtitle-lines", type=int, default=None, choices=[1, 2], help="Maximum subtitle lines per cue")
    p_bat.add_argument("--subtitle-font-size", type=int, default=None, help="Subtitle base font size at 1280px")
    p_bat.add_argument("--subtitle-chunking", choices=["sentence-aware", "fixed-chars"], default=None, help="Subtitle chunking strategy")
    p_bat.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default=None, help="Video encoder for rendering (default: auto)")
    p_bat.add_argument("--tts-engine", choices=["neural"], default="neural", help="Production requires local neural VieNeu-TTS")
    p_bat.add_argument("--tts-workers", type=int, default=2, help="TTS synthesis concurrent workers")
    p_bat.add_argument("--render-workers", type=int, default=2, help="Video render concurrent workers")
    p_bat.add_argument("--keep-temp", action=argparse.BooleanOptionalAction, default=None, help="Preserve intermediate clip files (default: True unless --cleanup)")
    p_bat.add_argument("--cleanup", action="store_true", help="Safely clean up temporary intermediate clips after verified final render")
    p_bat.add_argument("--kaggle", action="store_true", help="Offload batch processing (TTS + Rendering) to Kaggle Cloud GPU")

    # 10. youtube
    p_yt = subparsers.add_parser("youtube", help="YouTube metadata packaging, SEO, and video uploading", parents=[common_parent])
    p_yt.add_argument("youtube_action", choices=["metadata", "prepare", "upload", "verify", "auth", "accounts"], help="YouTube action: 'metadata', 'upload', 'verify', 'auth', or 'accounts'")
    p_yt.add_argument("-p", "--project", default=None, help="Project slug")
    p_yt.add_argument("-c", "--chapter", default="1", help="Chapter number (or 'feature')")
    p_yt.add_argument("--account", default=None, help="Target YouTube account/channel name (default: active default account)")
    p_yt.add_argument("-i", "--interactive", action="store_true", help="Interactively select target YouTube channel from numbered list")
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
    p_yt.add_argument("--privacy", choices=["public", "private", "unlisted"], default=None, help="Override the confirmed YouTube visibility")
    p_yt.add_argument("--thumbnail", default=None, help="Custom thumbnail image path (default: auto-detected from output/<slug>/chapter_<num>_thumbnail.jpg)")
    p_yt.add_argument("--no-captions", action="store_true", help="Skip uploading Vietnamese closed captions")
    p_yt.add_argument("--video-id", default=None, help="Target YouTube video ID for verification")

    # 11. clean
    p_cln = subparsers.add_parser("clean", help="Safely clean intermediate caches and temporary clips", parents=[common_parent])
    p_cln.add_argument("-p", "--project", required=True, help="Project slug")
    p_cln.add_argument("-c", "--chapter", default=None, help="Specific chapter number (default: all completed chapters)")
    p_cln.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True, help="Dry-run simulation mode without unlinking files (default: True)")
    p_cln.add_argument("--confirm-project", default=None, help="Explicit project slug confirmation required for actual deletion")
    p_cln.add_argument("--bypass-receipts", action="store_true", help="Dry-run inspection only; destructive use is rejected and cannot bypass receipts")
    p_cln.add_argument("--purge-raw", action="store_true", help="Also purge raw pages and panel crops after archive verification")

    # 14. cloud
    p_cld = subparsers.add_parser("cloud", help="Kaggle/Remote Cloud GPU runner for extreme speed", parents=[common_parent])
    cloud_subs = p_cld.add_subparsers(dest="cloud_action", title="cloud actions")

    p_cld_run = cloud_subs.add_parser("run", help="Run cloud production for a single chapter", parents=[common_parent])
    p_cld_run.add_argument("-p", "--project", default=None, help="Project slug")
    p_cld_run.add_argument("-c", "--chapter", default=None, help="Chapter number")
    p_cld_run.add_argument("--chapters", default=None, help="Comma-separated or range list of chapters")
    p_cld_run.add_argument("--provider", choices=["kaggle"], default="kaggle", help="Cloud provider (default: kaggle)")
    p_cld_run.add_argument("--no-gpu", action="store_true", help="Disable GPU and run CPU mode")
    p_cld_run.add_argument("--extreme", action="store_true", default=True, help="Enable extreme speed optimizations")
    p_cld_run.add_argument(
        "--cloud-policy",
        choices=["auto", "baseline_split", "dual_tts_shared_encode", "dual_tts_then_dual_encode", "one_tts_dual_encode"],
        default="auto",
        help="Dual-GPU execution policy override (default: auto)",
    )

    p_cld_info = cloud_subs.add_parser("info", help="Get cloud provider information and authentication status", parents=[common_parent])
    p_cld_info.add_argument("--provider", choices=["kaggle"], default="kaggle")

    p_cld_accounts = cloud_subs.add_parser("accounts", help="Manage local Kaggle tokens", parents=[common_parent])
    p_cld_accounts.add_argument("--account-action", choices=["list", "add", "remove", "default"], default="list")
    p_cld_accounts.add_argument("--account", default=None, help="Local account name")
    p_cld_accounts.add_argument("--token-file", default=None, help="Read token from a private file; otherwise prompt securely")

    p_cld_pull = cloud_subs.add_parser("pull", help="Pull results from cloud kernel", parents=[common_parent])
    p_cld_pull.add_argument("-p", "--project", default=None)
    p_cld_pull.add_argument("-c", "--chapter", default=None)

    p_cld_series = cloud_subs.add_parser(
        "produce-series",
        aliases=["produce_series"],
        help="Run Kaggle Dual-T4 single-session multi-chapter production",
        parents=[common_parent],
    )
    p_cld_series.add_argument("-p", "--project", required=True, help="Project slug")
    p_cld_series.add_argument("-c", "--chapters", required=True, help="Comma-separated or range list of chapters (e.g. '1,2,3' or '1-5')")
    p_cld_series.add_argument("--skip-preview", action="store_true", default=False, help="Compatibility only; cannot bypass Chapter 1 approval (G6)")
    p_cld_series.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True, help="Compile feature video across chapters (default: True)")
    p_cld_series.add_argument("--voice", default=None, help="Voice override")
    p_cld_series.add_argument("--speed", type=float, default=None, help="Speed override")
    p_cld_series.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default="auto", help="Video framing mode")
    p_cld_series.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=False, help="Burn subtitles directly onto video frames")
    p_cld_series.add_argument("--animation", action=argparse.BooleanOptionalAction, default=True, help="Enable camera motion animations")
    p_cld_series.add_argument("--no-gpu", action="store_true", help="Disable GPU and run CPU mode")
    p_cld_series.add_argument("--render-workers", type=int, default=2, help="Video clip render concurrent workers (default: 2)")
    p_cld_series.add_argument(
        "--cloud-policy",
        choices=["auto", "baseline_split", "dual_tts_shared_encode", "dual_tts_then_dual_encode", "one_tts_dual_encode"],
        default="auto",
        help="Dual-GPU execution policy override (default: auto)",
    )
    p_cld_series.add_argument(
        "--chunked",
        action="store_true",
        default=False,
        help="Split multi-chapter series into workload-sized chunks with verified incremental resume",
    )

    # 15. review-pack
    p_rp = subparsers.add_parser("review-pack", aliases=["review_pack"], help="Generate lightweight static human review pack", parents=[common_parent])
    p_rp.add_argument("-p", "--project", required=True, help="Project slug")
    p_rp.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_rp.add_argument("--format", choices=["md", "html", "json", "all"], default="all", help="Output format(s)")
    p_rp.add_argument("--out-dir", default=None, help="Custom output directory")

    # 16. archive
    p_arc = subparsers.add_parser("archive", help="Private Hugging Face Dataset archival and restoration", parents=[common_parent])
    p_arc.add_argument("archive_action", choices=["push", "verify", "restore"], help="Archive action: 'push', 'verify', or 'restore'")
    p_arc.add_argument("-p", "--project", required=True, help="Project slug")
    p_arc.add_argument("--repo-id", default=None, help="Hugging Face Dataset repo ID (default: Cuong2004/manga-recap-archive)")
    p_arc.add_argument("--token", default=None, help="Hugging Face API token override")
    p_arc.add_argument("--revision", default=None, help="Git commit SHA or branch for verify/restore")
    p_arc.add_argument("--target-dir", default=None, help="Local sandbox destination directory for restore")

    # 18. setup
    p_stp = subparsers.add_parser("setup", help="Record eight user-confirmed series settings", parents=[common_parent])
    p_stp.add_argument("setup_action", choices=["validate", "record", "show"], help="Setup action")
    p_stp.add_argument("-p", "--project", required=True, help="Project slug")
    p_stp.add_argument("--answers-json", "--answers", dest="answers_json", default=None, help="Path to answers JSON file or inline JSON string")
    p_stp.add_argument("--reviewer", default="user", help="Reviewer identifier (default: user)")

    # Canonical V6 stage commands (alongside full compatibility command set)
    # 19. prepare (crawl -> segment -> decide)
    p_prep = subparsers.add_parser("prepare", help="Prepare chapter assets (crawl, segment, decision routing)", parents=[common_parent])
    p_prep.add_argument("-p", "--project", required=True, help="Project slug")
    p_prep.add_argument("-c", "--chapter", required=True, help="Chapter number")
    p_prep.add_argument("-u", "--url", default=None, help="Source URL to scrape")
    p_prep.add_argument("--import-dir", default=None, help="Local directory to import raw images from")
    p_prep.add_argument("--max-panels", type=int, default=None, help="Max images limit")
    p_prep.add_argument("--verify", action="store_true", help="Perform strict image integrity and continuity verification")
    p_prep.add_argument("--target-width", type=int, default=1080, help="Target panel width")
    p_prep.add_argument("--force", action="store_true", help="Force re-segmentation")
    p_prep.add_argument("--skip-decide", action="store_true", help="Skip Decision API pass after segmentation")
    p_prep.add_argument("--workers", type=int, default=2, help="Concurrent Decision API requests (default: 2)")

    # 20. preview (validate script -> TTS -> preview render -> review pack)
    p_prv = subparsers.add_parser("preview", help="Produce Chapter 1 preview video and static review pack", parents=[common_parent])
    p_prv.add_argument("-p", "--project", required=True, help="Project slug")
    p_prv.add_argument("-c", "--chapter", default="1", help="Chapter number (must be '1')")
    p_prv.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default=None, help="Video encoder")
    p_prv.add_argument("--render-workers", type=int, default=None, help="Render concurrent worker count")
    p_prv.add_argument("--tts-workers", type=int, default=None, help="TTS concurrent worker count")
    p_prv.add_argument("--kaggle", action="store_true", help="Offload preview TTS/render to Kaggle Cloud GPU")
    p_prv.add_argument("--force", action="store_true", help="Re-synthesize TTS even if cached")

    # 21. approve (record human Chapter 1 preview approval bound to hashes)
    p_app = subparsers.add_parser("approve", help="Approve Chapter 1 preview with bound config and artifact hashes", parents=[common_parent])
    p_app.add_argument("-p", "--project", required=True, help="Project slug")
    p_app.add_argument("--reviewer", required=True, help="Reviewer identifier")
    p_app.add_argument("--config-fingerprint", required=True, help="Config fingerprint from review pack")
    p_app.add_argument("--artifact-hash", required=True, help="SHA-256 hash of preview.mp4 from review pack")

    # 22. produce (TTS + final render / Kaggle routed from setup compute choice)
    p_prod = subparsers.add_parser("produce", help="Produce approved chapter(s) via configured compute backend", parents=[common_parent])
    p_prod.add_argument("-p", "--project", required=True, help="Project slug")
    p_prod.add_argument("-c", "--chapters", default=None, help="Comma-separated or range list of chapters")
    p_prod.add_argument("--retry-chapter", default=None, help="Specific chapter number to retry")
    p_prod.add_argument("--compile", action="store_true", help="Auto-compile completed chapters into master recap video")
    p_prod.add_argument("--cleanup", action="store_true", help="Safely clean up temporary intermediate clips after verified final render")
    p_prod.add_argument("--kaggle", action=argparse.BooleanOptionalAction, default=None, help="Override compute setting from setup")
    p_prod.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox"], default=None, help="Video encoder")
    p_prod.add_argument("--render-workers", type=int, default=2, help="Video render concurrent workers")
    p_prod.add_argument("--tts-workers", type=int, default=2, help="TTS synthesis concurrent workers")
    p_prod.add_argument(
        "--cloud-policy",
        choices=["auto", "baseline_split", "dual_tts_shared_encode", "dual_tts_then_dual_encode", "one_tts_dual_encode"],
        default="auto",
        help="Kaggle Dual-GPU execution policy override (default: auto)",
    )
    p_prod.add_argument("--force", action="store_true", help="Force re-processing of completed chapters")

    # 23. publish (metadata, upload, and post-upload verification)
    p_pub = subparsers.add_parser("publish", help="Publish chapter or feature video to YouTube and verify live status", parents=[common_parent])
    p_pub.add_argument("-p", "--project", required=True, help="Project slug")
    p_pub.add_argument("-c", "--chapter", default="1", help="Chapter number (or 'feature')")
    p_pub.add_argument("--action", dest="publish_action", choices=["metadata", "prepare", "upload", "verify"], default="upload", help="Publish step (default: upload)")
    p_pub.add_argument("--account", default=None, help="Target YouTube account/channel name")
    p_pub.add_argument("-i", "--interactive", action="store_true", help="Interactively select target YouTube channel")
    p_pub.add_argument("--secrets", default=None, help="Path to client_secrets.json")
    p_pub.add_argument("--force", action="store_true", help="Force re-authentication")
    p_pub.add_argument("--video", default=None, help="Custom video file path override")
    p_pub.add_argument("--subtitles", default=None, help="Custom subtitles SRT file path override")
    p_pub.add_argument("--title", default=None, help="Video title override")
    p_pub.add_argument("--description", default=None, help="Video description override")
    p_pub.add_argument("--tags", default=None, help="Comma-separated tags")
    p_pub.add_argument("--privacy", choices=["public", "private", "unlisted"], default=None, help="Override confirmed YouTube visibility")
    p_pub.add_argument("--thumbnail", default=None, help="Custom thumbnail image path")
    p_pub.add_argument("--no-captions", action="store_true", help="Skip uploading Vietnamese closed captions")
    p_pub.add_argument("--video-id", default=None, help="Target YouTube video ID for verification")

    return parser


def _handle_prepare(args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.workflow import prepare_chapter
    return prepare_chapter(
        args.project,
        str(args.chapter),
        url=getattr(args, "url", None),
        import_dir=getattr(args, "import_dir", None),
        max_panels=getattr(args, "max_panels", None),
        verify=bool(getattr(args, "verify", False)),
        target_width=getattr(args, "target_width", 1080),
        force=bool(getattr(args, "force", False)),
        skip_decide=bool(getattr(args, "skip_decide", False)),
        workers=getattr(args, "workers", 2),
    )


def _handle_preview(args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.workflow import preview_chapter
    return preview_chapter(
        args.project,
        str(getattr(args, "chapter", "1")),
        encoder=getattr(args, "encoder", None),
        render_workers=getattr(args, "render_workers", None),
        tts_workers=getattr(args, "tts_workers", None),
        kaggle=bool(getattr(args, "kaggle", False)),
        force=bool(getattr(args, "force", False)),
    )


def _handle_approve(args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.workflow import approve_chapter_preview
    return approve_chapter_preview(
        args.project,
        reviewer=args.reviewer,
        config_fingerprint=args.config_fingerprint,
        artifact_hash=args.artifact_hash,
    )


def _handle_produce(args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.workflow import produce_chapters
    return produce_chapters(
        args.project,
        chapters=getattr(args, "chapters", None),
        retry_chapter=getattr(args, "retry_chapter", None),
        compile_after=bool(getattr(args, "compile", False)),
        cleanup=bool(getattr(args, "cleanup", False)),
        kaggle=getattr(args, "kaggle", None),
        encoder=getattr(args, "encoder", None),
        render_workers=getattr(args, "render_workers", 2),
        tts_workers=getattr(args, "tts_workers", 2),
        force=bool(getattr(args, "force", False)),
        cloud_policy=str(getattr(args, "cloud_policy", "auto") or "auto"),
    )


def _handle_publish(args: argparse.Namespace) -> Dict[str, Any]:
    from src.cli.youtube_cmd import handle_youtube
    args.youtube_action = getattr(args, "publish_action", "upload")
    return handle_youtube(args)


def _handle_review_approve(args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.human_review import approve_preview
    from src.core.project import get_project_dir
    return approve_preview(
        get_project_dir(args.project),
        args.reviewer,
        args.config_fingerprint,
        args.artifact_hash,
    ).model_dump(mode="json")


COMMAND_REGISTRY: Dict[str, Any] = {
    "prepare": _handle_prepare,
    "preview": _handle_preview,
    "approve": _handle_approve,
    "produce": _handle_produce,
    "publish": _handle_publish,
    "visual-plan": "src.cli.visual_plan_cmd:handle_visual_plan",
    "review-approve": _handle_review_approve,
    "inspect": "src.cli.inspect_cmd:handle_inspect",
    "validate": "src.cli.validate_cmd:handle_validate",
    "status": "src.cli.status_cmd:handle_status",
    "crawl": "src.cli.crawl_cmd:handle_crawl",
    "segment": "src.cli.segment_cmd:handle_segment",
    "repack": "src.cli.segment_cmd:handle_repack",
    "panel-inspect": "src.cli.segment_cmd:handle_panel_inspect",
    "decide": "src.cli.segment_cmd:handle_decide",
    "tts": "src.cli.tts_cmd:handle_tts",
    "tts_benchmark": "src.cli.tts_benchmark_cmd:handle_tts_benchmark",
    "render": "src.cli.render_cmd:handle_render",
    "compile": "src.cli.compile_cmd:handle_compile",
    "batch": "src.cli.batch_cmd:handle_batch",
    "youtube": "src.cli.youtube_cmd:handle_youtube",
    "clean": "src.cli.clean_cmd:handle_clean",
    "cloud": "src.cli.cloud_cmd:handle_cloud",
    "review-pack": "src.cli.review_pack:handle_review_pack",
    "review_pack": "src.cli.review_pack:handle_review_pack",
    "archive": "src.cli.archive_cmd:handle_archive",
    "setup": "src.cli.setup_cmd:handle_setup",
}


def execute_subcommand(subcommand: str, args: argparse.Namespace) -> Dict[str, Any]:
    from src.core.execution_policy import enforce_execution_policy
    enforce_execution_policy(subcommand, args)
    result = _dispatch_subcommand(subcommand, args)
    if isinstance(result, dict) and (result.get("status") == "error" or result.get("success") is False):
        message = str(result.get("error") or result.get("failed") or "Required command work failed")
        code = result.get("exit_code", 1)
        if code == 2:
            raise CliValidationError(message, data=result)
        if code == 4:
            raise DependencyNotFoundError(message)
        if code == 3:
            raise OSError(message)
        raise RuntimeError(message)
    return result


def _dispatch_subcommand(subcommand: str, args: argparse.Namespace) -> Dict[str, Any]:
    """Dispatches parsed arguments to registered command handlers."""
    target = COMMAND_REGISTRY.get(subcommand)
    if target is None:
        raise ValueError(f"Unknown subcommand: {subcommand}")
    if callable(target):
        return target(args)
    import importlib
    module_name, attr_name = target.split(":", 1)
    handler = getattr(importlib.import_module(module_name), attr_name)
    return handler(args)



def main(argv: Optional[List[str]] = None) -> int:
    """CLI execution entrypoint."""
    load_runtime_dotenv()
    start_time = time.monotonic()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    json_mode = "--json" in raw_args
    verbose = "-v" in raw_args or "--verbose" in raw_args

    # Configure logger to output exclusively to stderr
    configure_logging(verbose=verbose)

    # Detect subcommand early for error reporting
    detected_subcommand = "manga-recap"
    for arg in raw_args:
        if not arg.startswith("-") and arg in COMMAND_REGISTRY:
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
        args.json = json_mode
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

    except (DomainValidationError, CliValidationError, ValidationError, StorageCorruptError, InvalidCheckpointTransitionError) as e:
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
