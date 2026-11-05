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
    p_tts.add_argument("--engine", choices=["neural", "mock", "auto"], default="neural", help="TTS synthesis engine backend (default: neural)")
    p_tts.add_argument("--mock", action="store_true", help="Shortcut for --engine mock (testing only)")

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
    p_ren.add_argument("--video-mode", choices=["auto", "landscape_fit", "portrait_vertical", "portrait_padded"], default="auto", help="Video mode")
    p_ren.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=True, help="Burn subtitles")
    p_ren.add_argument("--animation", action=argparse.BooleanOptionalAction, default=True, help="Enable cinematic camera motion animations")
    p_ren.add_argument("--suffix", default="final", help="Output version suffix")
    p_ren.add_argument("--force", action="store_true", help="Ignore existing panel clips and force re-render")
    p_ren.add_argument("--keep-temp", action="store_true", help="Do not delete intermediate clips")
    p_ren.add_argument("--skip-review", action="store_true", help="Skip human review gate verification (deprecated for final render)")
    p_ren.add_argument("--approved", action="store_true", help="Explicitly approve Review Gate 1 (Voice) and Gate 2 (Preview) for automated/programmatic runs")
    p_ren.add_argument("--allow-placeholders", action="store_true", help="Allow dummy placeholder images/silence if assets are missing (testing only)")

    # 8. compile
    p_com = subparsers.add_parser("compile", help="Concatenate chapter videos into final recap", parents=[common_parent])
    p_com.add_argument("-p", "--project", required=True, help="Project slug")
    p_com.add_argument("--chapters", default=None, help="Comma-separated chapter numbers")
    p_com.add_argument("-o", "--output", default=None, help="Output final video path")
    p_com.add_argument("--approved", action="store_true", help="Explicitly approve Review Gate 1 (Voice) and Gate 2 (Preview) for automated/programmatic runs")
    p_com.add_argument("--extract-audio", action=argparse.BooleanOptionalAction, default=True, help="Extract master WAV")

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
            "crawl", "segment", "inspect", "validate", "tts", "tts_benchmark", "render", "compile", "status"
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
