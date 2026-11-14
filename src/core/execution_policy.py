"""Central, fail-closed policy applied before CLI handler imports or dispatch."""
from src.cli.exit_codes import CliValidationError
from src.core.series_setup import require_series_setup

COMMAND_REQUIREMENTS = {
    name: ("series_setup",) for name in (
        "crawl", "segment", "tts", "render", "compile", "batch", "produce",
        "cloud", "youtube", "thumbnail", "archive", "clean", "review-approve", "visual-plan",
    )
}


NON_PRODUCTION_COMMANDS = {"inspect", "validate", "status", "setup", "review-pack", "review_pack", "tts_benchmark"}


def enforce_execution_policy(command, args):
    if command in NON_PRODUCTION_COMMANDS:
        return
    if command not in COMMAND_REQUIREMENTS:
        raise CliValidationError(f"No execution policy declared for command: {command}")
    # These operations manage services rather than process a series.
    if command == "cloud" and getattr(args, "cloud_action", None) == "info":
        return
    if command == "youtube" and getattr(args, "youtube_action", None) in {"accounts", "auth"}:
        return
    slug = getattr(args, "project", None)
    if not slug:
        raise CliValidationError(f"{command}: a project and valid Q01-Q10 setup are required")
    require_series_setup(slug)
    production = command in {"batch", "produce", "compile"}
    production |= command == "tts" and str(getattr(args, "chapter", "1")) != "1"
    production |= command == "render" and (getattr(args, "quality", "final") == "final" or str(getattr(args, "chapter", "1")) != "1")
    production |= command == "cloud" and getattr(args, "cloud_action", "run") in {"produce-series", "produce_series"}
    production |= command == "youtube" and getattr(args, "youtube_action", None) == "upload"
    if production:
        from src.core.human_review import require_preview_approval
        from src.core.storage import get_project_dir
        require_preview_approval(get_project_dir(slug))



def resolve_legacy_chapter(chapter_id, chapter_number):
    """Resolve an existing identity exactly; never guess a default series."""
    from src.core.storage import get_projects_root, read_json
    matches = []
    for path in get_projects_root().glob(f"*/chapters/{chapter_number}/chapter.json"):
        if read_json(path).get("chapter_id") == chapter_id:
            matches.append(path.parents[2])
    if len(matches) != 1:
        raise CliValidationError("Legacy chapter identity is missing or ambiguous; use the canonical CLI with --project")
    project = matches[0]
    require_series_setup(project.name, project_dir=project)
    return project
