"""Generate or explicitly regenerate a canonical plan after source/audio edits."""
from src.core.execution_policy import enforce_execution_policy
from src.core.storage import get_project_dir, load_manifest, load_recap_script, read_json
from src.render.visual_policy import generate_visual_plan, resolve_visual_plan
from src.render.artifact_validation import fingerprint


def handle_visual_plan(args):
    enforce_execution_policy("visual-plan", args)
    root = get_project_dir(args.project)
    chapter = root / "chapters" / str(args.chapter)
    settings = read_json(root / "project.json")["render_settings"]
    kwargs = dict(target_width=settings["target_width"], target_height=settings["target_height"], fps=settings["fps"],
                  motion_enabled=settings["render_pan_mode"] == "animation")
    manifest, script = load_manifest(chapter), load_recap_script(chapter)
    path = chapter / "visual_plan.json"
    if args.regenerate:
        plan = generate_visual_plan(chapter, manifest, script, **kwargs)
        if path.exists():
            path.replace(chapter / f"visual_plan.stale.{fingerprint(path)[:16]}.json")
    else:
        plan = resolve_visual_plan(chapter, manifest, script, **kwargs)
    plan.save(path)
    return dict(path=str(path), input_fingerprint=plan.input_fingerprint, shots=len(plan.shots), duration=plan.total_duration_sec)
