from argparse import Namespace

import pytest

from src.cli.clean_cmd import handle_clean
from src.cli.exit_codes import CliValidationError
from src.core.storage import get_project_dir


def test_clean_requires_final_and_keeps_exports():
    chapter = get_project_dir("series") / "chapters" / "1"
    clip = chapter / "audio" / "clips" / "voice.wav"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"temporary")
    args = Namespace(project="series", chapter="1", dry_run=True)
    assert handle_clean(args)["skipped_chapters"]
    video = chapter / "video" / "final.mp4"
    subtitle = chapter / "subtitles" / "final.srt"
    for path in (video, subtitle):
        path.parent.mkdir()
        path.write_bytes(b"test fixture")
    assert handle_clean(args)["cleaned_files_count"] == 1
    assert clip.exists()

    args.dry_run = False
    with pytest.raises(CliValidationError, match="confirm-project"):
        handle_clean(args)
    args.confirm_project = "series"
    assert handle_clean(args)["cleaned_files_count"] == 1
    assert not clip.exists()
    assert video.is_file() and subtitle.is_file()
