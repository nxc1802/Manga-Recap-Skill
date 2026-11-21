"""Self-contained source/project payload inventory with fail-closed hash checking."""
import tarfile
import tempfile
import shutil
from pathlib import Path
from src.core.storage import atomic_write_json, calculate_sha256, read_json
from src.core.utils import assert_file_nonempty, get_wav_duration


def verify_payload_manifest(root, expected_manifest_hash):
    root = Path(root).resolve()
    path = root / "payload_manifest.json"
    if calculate_sha256(path) != expected_manifest_hash:
        raise ValueError("Payload manifest hash mismatch")
    manifest = read_json(path)
    if manifest.get("schema_version") != "4.1.0" or not manifest.get("files"):
        raise ValueError("Invalid payload manifest")
    actual = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Payload symlinks are forbidden")
        if entry.is_file() and entry != path:
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(manifest["files"]):
        raise ValueError("Payload inventory differs from manifest")
    for relative, digest in manifest["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or calculate_sha256(target) != digest:
            raise ValueError(f"Missing, unsafe or altered payload file: {relative}")
    for required in ("src/core/visual_contract.py", "src/render/fast_renderer.py", "requirements.txt", "project/project.json", "project/series_setup_answers.json"):
        if required not in actual:
            raise ValueError(f"Required runtime asset is absent: {required}")
    return manifest


def package_payload(project_dir, chapter_number, output, job_type, *, series=False):
    from src.core.human_review import require_preview_approval, require_voice_approval
    from src.core.series_setup import require_series_setup
    from src.core.storage import load_manifest, load_recap_script
    from src.render.visual_policy import validate_narration_evidence

    root = Path(project_dir).resolve()
    slug = read_json(root / "project.json")["slug"]
    require_series_setup(slug, project_dir=root)
    require_voice_approval(root)
    if job_type not in {"tts", "render", "produce"}:
        raise ValueError("Unknown cloud job type")
    chapters = [str(chapter_number)] if not series else [str(number) for number in chapter_number]
    if not chapters or len(set(chapters)) != len(chapters) or not all(c.isdecimal() and c.isascii() for c in chapters):
        raise ValueError("Expected distinct numeric chapter IDs")
    needs_preview = series or any(number != "1" for number in chapters)
    if needs_preview:
        require_preview_approval(root)
    for number in chapters:
        chapter = root / "chapters" / number
        script, source_manifest = load_recap_script(chapter), load_manifest(chapter)
        validate_narration_evidence(chapter, script, source_manifest)
        if script.project_slug != slug or script.chapter_number != number:
            raise ValueError("Payload chapter/project identity mismatch")
        for name in ("chapter.json", "script.json", "manifest.json"):
            assert_file_nonempty(chapter / name)
    source_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="manga-payload-") as temp:
        stage = Path(temp)
        files = {}
        for source in (source_root / "src").rglob("*"):
            if source.suffix not in {".py", ".json"} or not source.is_file():
                continue
            files[source.relative_to(source_root).as_posix()] = source
        for name in ("requirements.txt", "pyproject.toml"):
            files[name] = source_root / name
        for name in ("project.json", "series_setup_answers.json", "voice_selection.json", "preview_approval.json"):
            if (root / name).is_file():
                files[f"project/{name}"] = root / name
        wm_dir = root.parent.parent / "watermarks"
        if wm_dir.is_dir():
            for wm_file in wm_dir.iterdir():
                if wm_file.is_file() and not wm_file.is_symlink() and wm_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    files[f"data/watermarks/{wm_file.name}"] = wm_file
        allowed_roots = {"raw", "panels", "audio", "subtitles", "contact_sheets"}
        allowed_files = {"chapter.json", "script.json", "manifest.json", "visual_plan.json", "thumbnail.jpg", "thumbnail.png"}
        for number in dict.fromkeys((["1"] if needs_preview else []) + chapters):
            chapter = root / "chapters" / number
            if not chapter.is_dir():
                raise ValueError(f"Missing chapter payload: {number}")
            for path in chapter.rglob("*"):
                rel = path.relative_to(chapter)
                if path.is_symlink():
                    raise ValueError(f"Symlink in payload: {path.name}")
                if path.is_file() and (rel.parts[0] in allowed_roots or rel.as_posix() in allowed_files or (number == "1" and rel.as_posix() == "video/preview.mp4")):
                    if path.suffix in {".lock", ".tmp"} or path.name.startswith("."):
                        continue
                    files[f"project/chapters/{number}/{rel.as_posix()}"] = path
        bgm = read_json(root / "project.json").get("audio_settings", {}).get("bgm_path")
        if bgm:
            path = root / bgm
            if Path(bgm).is_absolute() or not path.resolve().is_relative_to(root):
                raise ValueError("Cloud BGM must be a portable project-relative asset")
            files[f"project/{bgm}"] = path
        for relative, path in files.items():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing/unsafe runtime asset: {relative}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        manifest = dict(schema_version="4.1.0", project_slug=slug, chapters=chapters, series=series, job_type=job_type,
                        files={relative: calculate_sha256(stage / relative) for relative in sorted(files)})
        atomic_write_json(stage / "payload_manifest.json", manifest)
        digest = calculate_sha256(stage / "payload_manifest.json")
        verify_payload_manifest(stage, digest)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(stage).as_posix())
    return output, digest


def export_results(project, manifest, manifest_hash, output, *, compile_feature=False):
    import argparse
    from src.core.storage import get_output_dir, load_recap_script, stage_chapter_export
    from src.render.chapter_renderer import resolve_audio

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    chapters = manifest["chapters"]
    render = manifest["job_type"] != "tts"
    for number in chapters:
        chapter = project / "chapters" / number
        paths = [chapter / "script.json"]
        unchanged_audio = manifest["job_type"] == "render" or (manifest["series"] and number == "1")
        if not unchanged_audio:
            paths.extend(resolve_audio(chapter, seg) for seg in load_recap_script(chapter).segments)
        if render:
            quality = "preview" if number == "1" and not manifest["series"] else "final"
            video, subtitle = (chapter / "video" / f"{quality}.mp4", chapter / "subtitles" / f"{quality}.srt")
            paths.extend([video, subtitle, chapter / "visual_plan.json"])
            if compile_feature:
                stage_chapter_export(manifest["project_slug"], number, video, subtitle)
        for path in paths:
            assert_file_nonempty(path)
            dest = output / "chapters" / number / path.relative_to(chapter)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    feature = {}
    if compile_feature:
        from src.cli.compile_cmd import handle_compile
        result = handle_compile(argparse.Namespace(project=manifest["project_slug"], chapters=",".join(chapters), output=None))
        prefix = get_output_dir(manifest["project_slug"])
        for key, value in result.items():
            if isinstance(value, str):
                path = Path(value)
                if path.is_file() and (path.resolve().is_relative_to(prefix.resolve()) or key == "feature_timeline"):
                    dest = output / "feature" / path.name
                    dest.parent.mkdir(exist_ok=True)
                    shutil.copy2(path, dest)
                    feature[key] = path.name
    atomic_write_json(output / "result_manifest.json", {
        "project_slug": manifest["project_slug"], "chapters": chapters,
        "job_type": manifest["job_type"], "series": manifest["series"],
        "payload_manifest_sha256": manifest_hash, "feature": feature,
    })


def sync_results(project, chapters, downloaded, job, manifest_hash, expected_inputs, *, series=False):
    from src.core.schemas import CheckpointStatus, HumanReviewStatus, StageCheckpoint, StageType, utc_iso_now
    from src.core.storage import acquire_lock, get_output_dir, load_chapter_metadata, load_recap_script, stage_chapter_export
    from src.core.visual_contract import VisualPlan
    from src.render.chapter_renderer import copy_artifact, resolve_audio

    project, downloaded = Path(project).resolve(), Path(downloaded).resolve()
    if (downloaded / "results").is_dir():
        downloaded /= "results"
    for path in downloaded.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in cloud result: {path}")
    receipt = read_json(downloaded / "result_manifest.json")
    slug = read_json(project / "project.json")["slug"]
    if not manifest_hash or any(receipt.get(key) != value for key, value in {
        "project_slug": slug, "chapters": chapters, "job_type": job,
        "series": series, "payload_manifest_sha256": manifest_hash,
    }.items()):
        raise ValueError("Cloud receipt does not match the dispatched job")
    if not expected_inputs:
        raise ValueError("Missing cloud input snapshot")
    for relative, digest in expected_inputs.items():
        path = (project / relative).resolve()
        if not path.is_relative_to(project):
            raise ValueError(f"Unsafe cloud input path: {relative}")
        try:
            local_digest = calculate_sha256(path)
        except FileNotFoundError:
            local_digest = None
        if local_digest != digest:
            incoming = downloaded / relative
            parts = Path(relative).parts
            published = len(parts) >= 3 and parts[:2] in [("chapters", number) for number in chapters] and (
                parts[2] in {"script.json", "visual_plan.json", "audio", "subtitles", "video"}
            )
            if not (published and local_digest and incoming.is_file() and calculate_sha256(incoming) == local_digest):
                raise ValueError(f"Local input changed during the cloud job: {relative}")

    pending = []
    for number in chapters:
        local = project / "chapters" / number
        incoming = downloaded / "chapters" / number
        script = load_recap_script(incoming)
        original = load_recap_script(local)
        source_fields = {"seq_id", "narrator_text", "panel_ids", "evidence_panel_ids", "camera_motion",
                         "focus_box", "emotion", "emotion_cue"}
        if not script.segments or script.project_slug != slug or script.chapter_number != number or [
            s.model_dump(include=source_fields) for s in script.segments
        ] != [s.model_dump(include=source_fields) for s in original.segments]:
            raise ValueError(f"Cloud narration changed for chapter {number}")
        paths = []
        if job != "render" and not (series and number == "1"):
            for segment in script.segments:
                path = resolve_audio(incoming, segment)
                if segment.duration is None or abs(get_wav_duration(path) - segment.duration) > 0.001:
                    raise ValueError(f"Cloud WAV duration differs for {segment.seq_id}")
                paths.append(path)
        elif script != original:
            raise ValueError(f"Render-only job changed audio metadata for chapter {number}")
        quality = "preview" if number == "1" and not series else "final"
        plan = None
        if job != "tts":
            plan = VisualPlan.load(incoming / "visual_plan.json")
            paths += [incoming / "video" / f"{quality}.mp4", incoming / "subtitles" / f"{quality}.srt"]
        for path in paths:
            assert_file_nonempty(path)
        pending.append((number, local, incoming, script, paths, quality, plan))

    feature_paths = {}
    for key, filename in receipt.get("feature", {}).items():
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise ValueError("Invalid feature filename")
        path = downloaded / "feature" / filename
        assert_file_nonempty(path)
        feature_paths[key] = path

    results = []
    for number, local, incoming, script, paths, quality, plan in pending:
        with acquire_lock(local / "chapter.json"):
            script_path = local / "script.json"
            local_script = load_recap_script(local)
            script_changed = local_script != script
            if script_changed and expected_inputs.get(script_path.relative_to(project).as_posix()) != calculate_sha256(script_path):
                raise ValueError(f"Local narration changed during cloud sync for chapter {number}")
            metadata = load_chapter_metadata(local)
            for source in paths:
                dest = local / source.relative_to(incoming)
                if not dest.resolve().is_relative_to(local.resolve()):
                    raise ValueError(f"Unsafe cloud result destination: {dest}")
                copy_artifact(source, dest)
            if script_changed:
                atomic_write_json(script_path, script)
            def checkpoint(stage, status, artifacts=()):
                entry = metadata.stages.setdefault(stage, StageCheckpoint(stage=stage))
                entry.status = status
                entry.completed_at = utc_iso_now() if status == CheckpointStatus.COMPLETED else None
                entry.error_message = None
                entry.artifacts = list(artifacts)

            for stage in (StageType.TTS, StageType.AUDIO):
                checkpoint(stage, CheckpointStatus.COMPLETED, (s.audio_path for s in script.segments))
            result = dict(status="success", job_type=job, project_slug=slug, chapter_number=number)
            if plan:
                atomic_write_json(local / "visual_plan.json", plan)
                video, subtitle = paths[-2:]
                video, subtitle = local / video.relative_to(incoming), local / subtitle.relative_to(incoming)
                checkpoint(StageType.RENDER, CheckpointStatus.COMPLETED,
                           (video.relative_to(local).as_posix(), subtitle.relative_to(local).as_posix(), "visual_plan.json"))
                metadata.current_stage = StageType.RENDER
                metadata.technical_status = CheckpointStatus.COMPLETED
                metadata.overall_status = CheckpointStatus.NEEDS_REVIEW if quality == "preview" else CheckpointStatus.COMPLETED
                metadata.human_review_status = HumanReviewStatus.APPROVED if series and number == "1" else HumanReviewStatus.UNREVIEWED
                metadata.stats.video_path = str(video)
                metadata.stats.final_video_duration_seconds = plan.total_duration_sec
                if quality == "preview":
                    old_final = local / "video" / "final.mp4"
                    if old_final.is_file():
                        old_final.replace(old_final.with_name("final.previous.mp4"))
                result.update(video_path=str(video), subtitle_path=str(subtitle), final_video=str(video),
                              subtitles=str(subtitle), duration_seconds=plan.total_duration_sec)
            elif script_changed:
                (local / "visual_plan.json").unlink(missing_ok=True)
                for name in ("video/final.mp4", "video/preview.mp4", "subtitles/final.srt", "subtitles/preview.srt"):
                    path = local / name
                    if path.is_file():
                        path.replace(path.with_suffix(".previous"))
                for stage in (StageType.RENDER, StageType.EXPORT):
                    checkpoint(stage, CheckpointStatus.PENDING)
                metadata.overall_status = metadata.technical_status = CheckpointStatus.PENDING
                metadata.human_review_status = HumanReviewStatus.UNREVIEWED
                metadata.current_stage = StageType.TTS
            atomic_write_json(local / "chapter.json", metadata)
        if quality == "final" and plan:
            staged = stage_chapter_export(slug, number, video, subtitle)
            result["export_paths"] = {key: str(value) if value else None for key, value in staged.items()}
        results.append(result)

    feature = {}
    for key, source in feature_paths.items():
        dest = get_output_dir(slug) / source.name
        copy_artifact(source, dest)
        feature[key] = str(dest)
        if key == "feature_timeline":
            copy_artifact(source, project / "feature_timeline.json")
    if not series:
        return results[0]
    return dict(status="success", job_type="produce_series", project_slug=slug, chapters=chapters,
                published={item["chapter_number"]: item for item in results}, feature=feature,
                compiled_video=feature.get("compiled_video") or feature.get("export_video_path"))
