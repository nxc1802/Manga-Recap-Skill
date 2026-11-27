"""Review-pack CLI command handler delegating to core review pack generation."""

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.response import print_response
from src.core.review_pack import (
    build_review_pack_data,
    create_review_pack,
    generate_markdown_review_pack,
)

__all__ = [
    "build_review_pack_data",
    "generate_markdown_review_pack",
    "create_review_pack",
    "handle_review_pack",
    "main",
]


def handle_review_pack(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap review-pack`` by delegating to core review pack generation."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug:
        raise CliValidationError("Project slug is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number is required for generating a review pack.")

    out_dir_arg = getattr(args, "out_dir", None)
    return create_review_pack(
        project_slug,
        str(chapter_num),
        fmt=getattr(args, "format", "all"),
        out_dir=Path(out_dir_arg) if out_dir_arg else None,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone module entrypoint for generating chapter review packs."""
    parser = argparse.ArgumentParser(
        prog="python -m src.cli.review_pack",
        description="Generate lightweight static human review pack for a manga chapter",
    )
    parser.add_argument("-p", "--project", required=True, help="Project slug")
    parser.add_argument("-c", "--chapter", required=True, help="Chapter number")
    parser.add_argument(
        "--format",
        choices=["md", "json", "all"],
        default="all",
        help="Output format(s)",
    )
    parser.add_argument("--out-dir", default=None, help="Custom output directory")
    parser.add_argument("--json", action="store_true", help="Print JSON output envelope")

    args = parser.parse_args(argv)
    try:
        data = handle_review_pack(args)
        if args.json:
            print_response("review-pack", data=data)
        else:
            print(f"[+] Review Pack generated for {args.project} Chapter {args.chapter}:")
            for fmt_name, path_str in data["generated_files"].items():
                print(f"    - {fmt_name.upper()}: {path_str}")
        return ExitCode.SUCCESS
    except Exception as exc:
        if args.json:
            print_response(
                "review-pack",
                error={"code": type(exc).__name__, "message": str(exc)},
                exit_code=ExitCode.INPUT_ERROR,
            )
        else:
            print(f"[-] Error generating review pack: {exc}")
        return ExitCode.INPUT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
