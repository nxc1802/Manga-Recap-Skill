"""
src/crawler/verifier.py
ImageVerifier: Multi-stage image integrity and chapter asset validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageStat

from src.core.storage import calculate_sha256


@dataclass
class ImageVerificationReport:
    """Report detailing raw chapter image verification results."""
    is_valid: bool
    total_pages: int
    corrupt_pages: List[str] = field(default_factory=list)
    duplicate_pages: List[str] = field(default_factory=list)
    missing_indices: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class ImageVerifier:
    """Verifies single image files and chapter raw image batches."""

    IMAGE_SIGNATURES = [
        (b"\xff\xd8\xff", "jpeg"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"GIF8", "gif"),
    ]

    @classmethod
    def verify_image(cls, file_path: Path, strict: bool = False) -> Tuple[bool, Optional[str]]:
        """
        Verifies single image file on disk.
        In strict=False (default for unit test backward compatibility), verifies file exists and >0 bytes.
        In strict=True (used during online crawl and explicit verify), executes multi-stage validation:
        1. file size >= 1024 bytes
        2. magic bytes check (not HTML/JSON error document, valid image header)
        3. PIL.Image decode & verification
        4. min dimensions (>= 150x150)
        5. variance check (non-solid color placeholder)
        """
        if not file_path.exists():
            return False, f"File does not exist: {file_path}"

        size = file_path.stat().st_size
        if size == 0:
            return False, f"File is empty (0 bytes): {file_path.name}"

        if not strict:
            return True, None

        # --- Strict checks ---
        # 1. Magic bytes & anti-bot HTML challenge or JSON error check
        with open(file_path, "rb") as f:
            header = f.read(512)

        header_lower = header.lower()
        if (
            b"<!doctype" in header_lower
            or b"<html" in header_lower
            or b"<script" in header_lower
            or header.strip().startswith(b"{\"error\"")
        ):
            return False, f"File is an HTML/JSON error page rather than an image: {file_path.name}"

        # 2. File size threshold
        if size < 1024:
            return False, f"File too small ({size} bytes < 1KB threshold): {file_path.name}"


        is_webp = len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        is_known_sig = any(header.startswith(sig) for sig, _ in cls.IMAGE_SIGNATURES) or is_webp

        if not is_known_sig:
            return False, f"Unrecognized image magic header bytes: {file_path.name}"

        # PIL decode & verify
        try:
            with Image.open(file_path) as img:
                img.verify()

            # Re-open to read dimensions and variance (verify() invalidates image object)
            with Image.open(file_path) as img:
                w, h = img.size
                if w < 150 or h < 150:
                    return False, f"Image dimensions too small ({w}x{h} < 150x150): {file_path.name}"

                # Solid color / blank placeholder check
                gray = img.convert("L")
                stat = ImageStat.Stat(gray)
                var = stat.var[0] if stat.var else 0.0
                if var < 2.0:
                    return False, f"Image is solid color or blank placeholder (variance {var:.2f} < 2.0): {file_path.name}"
        except Exception as e:
            return False, f"PIL decode failure on {file_path.name}: {e}"

        return True, None

    @classmethod
    def verify_chapter_raw_images(
        cls,
        raw_dir: Path,
        strict: bool = False,
    ) -> ImageVerificationReport:
        """
        Validates batch of raw images in chapter directory:
        - Checks file corruptions
        - Checks sequence continuity (page_001, page_002, ...)
        - Checks identical consecutive duplicate ads
        """
        if not raw_dir.exists():
            return ImageVerificationReport(
                is_valid=False,
                total_pages=0,
                errors=[f"Raw directory does not exist: {raw_dir}"],
            )

        valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
        files = sorted([f for f in raw_dir.iterdir() if f.is_file() and f.suffix.lower() in valid_exts])

        if not files:
            return ImageVerificationReport(
                is_valid=False,
                total_pages=0,
                errors=["No raw image files found in chapter raw directory"],
            )

        corrupt_pages = []
        errors = []
        duplicate_pages = []
        missing_indices = []

        # 1. Verify each file
        prev_hash = None
        indices = []

        seq_regex = re.compile(r"page_(\d+)")

        for f in files:
            ok, err = cls.verify_image(f, strict=strict)
            if not ok:
                corrupt_pages.append(f.name)
                if err:
                    errors.append(err)

            # Sequence index tracking
            m = seq_regex.search(f.stem)
            if m:
                indices.append(int(m.group(1)))

            # Deduplication check
            curr_hash = calculate_sha256(f)
            if prev_hash and curr_hash == prev_hash:
                duplicate_pages.append(f.name)
            prev_hash = curr_hash

        # 2. Check continuity if sequence numbers are present
        if indices:
            indices_sorted = sorted(indices)
            expected = list(range(1, max(indices_sorted) + 1))
            missing = sorted(set(expected) - set(indices_sorted))
            if missing:
                missing_indices = missing
                errors.append(f"Missing sequence page numbers: {missing}")

        is_valid = len(corrupt_pages) == 0 and len(missing_indices) == 0

        return ImageVerificationReport(
            is_valid=is_valid,
            total_pages=len(files),
            corrupt_pages=corrupt_pages,
            duplicate_pages=duplicate_pages,
            missing_indices=missing_indices,
            errors=errors,
        )
