"""CLI handler: manga-recap clean."""

from src.core.cleanup import clean_project_chapters


def handle_clean(args):
    return clean_project_chapters(
        args.project,
        chapter=getattr(args, "chapter", None),
        dry_run=args.dry_run,
        confirm_project=getattr(args, "confirm_project", None),
        bypass_receipts=getattr(args, "bypass_receipts", False),
        purge_raw=getattr(args, "purge_raw", False),
    )
