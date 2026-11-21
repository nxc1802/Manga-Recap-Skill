"""Remove chapter intermediates after final export."""
from src.cli.exit_codes import CliValidationError
from src.core.schemas import ArchiveReceipt, CheckpointStatus, StageType
from src.core.storage import acquire_lock, calculate_sha256, get_project_dir, load_chapter_metadata, read_json
from src.core.utils import assert_file_nonempty


def _archived_files(root, slug):
    receipt = ArchiveReceipt.model_validate(read_json(root / "archive_receipt.json"))
    manifest_path = root / "archive_manifest.json"
    manifest = read_json(manifest_path)
    if (receipt.status != "verified" or receipt.verification_revision != receipt.commit_sha
            or receipt.project_slug != slug or manifest.get("project_slug") != slug
            or manifest.get("repo_id") != receipt.repo_id
            or calculate_sha256(manifest_path) != receipt.manifest_sha256):
        raise CliValidationError("Verify the current archive before purging raw pages and panels")
    return {entry["relative_path"]: entry["sha256"] for entry in manifest["files"]}


def handle_clean(args):
    slug = args.project
    root = get_project_dir(slug).resolve()
    if not root.is_dir():
        raise CliValidationError(f"Project not found: {slug}")
    dry = args.dry_run
    purge_raw = getattr(args, "purge_raw", False)
    if not dry and getattr(args, "confirm_project", None) != slug:
        raise CliValidationError(f"Specify --confirm-project {slug} to delete files")
    if not dry and getattr(args, "bypass_receipts", False):
        raise CliValidationError("--bypass-receipts is for dry runs")
    archived = _archived_files(root, slug) if purge_raw and not dry else {}
    chapter_id = getattr(args, "chapter", None)
    chapters = root / "chapters"
    if chapter_id is not None and not str(chapter_id).isdigit():
        raise CliValidationError("Chapter must be numeric")
    targets = [chapters / str(chapter_id)] if chapter_id is not None else sorted(chapters.glob("*"))
    patterns = ("audio/clips/*.wav", "audio/speech_master*.wav", "audio/mixed.m4a",
                "video/clips/*", "video/raw_video_*.mp4", "video/*_v[0-9]*.mp4",
                "video/final.previous.mp4", "video/final.stale.*.mp4",
                "video/.*.partial.mp4", "video/*.previous", "subtitles/*.previous",
                "**/*concat_list*.txt", "**/*.tmp")
    if purge_raw:
        patterns += ("raw/*", "panels/*")
    cleaned, skipped, names = [], [], []
    size = 0
    for chapter in targets:
        if not chapter.name.isdigit() or not chapter.is_dir() or chapter.is_symlink():
            continue
        try:
            assert_file_nonempty(chapter / "video" / "final.mp4")
            assert_file_nonempty(chapter / "subtitles" / "final.srt")
        except FileNotFoundError as exc:
            skipped.append({"chapter": chapter.name, "reason": str(exc)})
            continue
        with acquire_lock(chapter / "chapter.json"):
            if (chapter / "chapter.json").is_file():
                states = load_chapter_metadata(chapter).stages
                busy = any(states[stage].status == CheckpointStatus.RUNNING
                           for stage in (StageType.AUDIO, StageType.TTS, StageType.RENDER) if stage in states)
                if busy:
                    skipped.append({"chapter": chapter.name, "reason": "Audio or render is running"})
                    continue
            candidates = sorted({path for pattern in patterns for path in chapter.glob(pattern)
                                 if path.is_file() and not path.is_symlink()
                                 and path.resolve().is_relative_to(chapter)
                                 and (purge_raw or path.relative_to(chapter).parts[0] not in {"raw", "panels"})})
            if purge_raw and not dry:
                for path in candidates:
                    if path.relative_to(chapter).parts[0] in {"raw", "panels"}:
                        if archived.get(path.relative_to(root).as_posix()) != calculate_sha256(path):
                            raise CliValidationError(f"Archive does not cover current source file: {path}")
            for path in candidates:
                size += path.stat().st_size
                names.append(path.relative_to(root).as_posix())
                if not dry:
                    path.unlink()
        cleaned.append(chapter.name)
    return dict(project_slug=slug, dry_run=dry, chapters_cleaned=cleaned, skipped_chapters=skipped,
                cleaned_files=names, cleaned_files_count=len(names), reclaimed_bytes=size,
                reclaimed_mb=round(size / 2**20, 2), reclaimed_gb=round(size / 2**30, 2))
