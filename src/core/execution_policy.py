"""Central, fail-closed policy applied before CLI handler imports or dispatch."""
from src.core.errors import DomainValidationError
from src.core.project import get_project_dir
from src.core.series_setup import require_series_setup

COMMAND_REQUIREMENTS = {
    name: ("series_setup",) for name in (
        "crawl", "segment", "repack", "panel-inspect", "decide", "prepare",
        "tts", "render", "preview", "compile", "produce",
        "cloud", "youtube", "publish", "archive", "clean", "approve", "visual-plan",
    )
}


NON_PRODUCTION_COMMANDS = {"inspect", "validate", "status", "setup", "review-pack"}


def enforce_execution_policy(command, args):
    if command in NON_PRODUCTION_COMMANDS:
        return
    if command not in COMMAND_REQUIREMENTS:
        raise DomainValidationError(f"No execution policy declared for command: {command}")
    # These operations manage services rather than process a series.
    if command == "cloud" and getattr(args, "cloud_action", None) in {"info", "accounts"}:
        return
    if command == "youtube" and getattr(args, "youtube_action", None) in {"accounts", "auth"}:
        return
    slug = getattr(args, "project", None)
    if not slug:
        raise DomainValidationError(f"{command}: a project and eight confirmed setup answers are required")
    require_series_setup(slug)
    production = command in {"compile", "produce"}
    production |= command == "publish" and getattr(args, "publish_action", "upload") == "upload"
    production |= command == "tts" and str(getattr(args, "chapter", "1")) != "1"
    production |= command == "render" and (getattr(args, "quality", "final") == "final" or str(getattr(args, "chapter", "1")) != "1")
    production |= command == "cloud" and getattr(args, "cloud_action", "run") == "produce-series"
    production |= command == "youtube" and getattr(args, "youtube_action", None) == "upload"
    if production:
        from src.core.human_review import require_preview_approval
        require_preview_approval(get_project_dir(slug))
