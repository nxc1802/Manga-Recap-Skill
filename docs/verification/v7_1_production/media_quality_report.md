# V7.1 — Media & Approval Quality Report (P4, P5, P7.1, P8.1)

**Candidate SHA:** `5d8ca077a4517d76395d22e19abfb8a39eb0c85b`  
**Narrator / Speed / Subtitles / BGM:** `Ngọc Huyền` (`single_narrator`, real VieNeu-TTS v3 Turbo `neural`), `1.0x`, Vietnamese subtitles burned `ON`, no BGM  

---

## 1. Technical Media Verification (`ffprobe` + Full FFmpeg Decode)

Every video below passed full decode (`ffmpeg -v error -xerror -i <video> -f null -` -> `rc=0`, `stderr=''`):

| Artifact | Backend | Resolution | Codecs / FPS | Video Dur | Audio Dur | A/V Diff | Size | Decode |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `v71-accept-local` Ch. 1 `preview.mp4` | Local (`libx264`) | `1280x720` | `h264 / aac 48kHz / 30fps` | `188.397 s` | `188.400 s` | `0.003 s` | `25.01 MB` | **PASS (`0`)** |
| `v71-accept-local` Ch. 2 `final.mp4` | Local (`libx264`) | `1280x720` | `h264 / aac 48kHz / 30fps` | `138.864 s` | `138.880 s` | `0.016 s` | — | **PASS (`0`)** |
| `v71-accept-local` Feature `full_recap.mp4` | Local compile | `1280x720` | `h264 / aac 48kHz / 30fps` | `327.264 s` | `327.301 s` | `0.037 s` | — | **PASS (`0`)** |
| `v71-accept-local` Master `full_recap.wav` | Local compile | — | `pcm_s16le / 48kHz` | — | `327.339 s` | — | `62.85 MB` | **PASS** |
| `v71-accept-kaggle` Ch. 1 `preview.mp4` | Kaggle Dual-T4 (`h264_nvenc`) | `1920x1080` | `h264 / aac 48kHz / 30fps` | `187.833 s` | `187.840 s` | `0.007 s` | `51.43 MB` | **PASS (`0`)** |
| `v71-accept-kaggle` Ch. 2 `final.mp4` | Kaggle Dual-T4 (`h264_nvenc`) | `1920x1080` | `h264 / aac 48kHz / 30fps` | `137.433 s` | `137.440 s` | `0.007 s` | — | **PASS (`0`)** |
| `v71-accept-kaggle` Feature `full_recap.mp4` | Kaggle + Compile | `1920x1080` | `h264 / aac 48kHz / 30fps` | `325.207 s` | `325.301 s` | `0.094 s` | — | **PASS (`0`)** |
| `v71-accept-kaggle` Master `full_recap.wav` | Kaggle + Compile | — | `pcm_s16le / 48kHz` | — | `325.323 s` | — | `62.46 MB` | **PASS** |

- **Subtitles:** Ch. 1 (`60` SRT cues), Ch. 2 (`42` SRT cues), sentence-aware chunking, monotonic timestamps.
- **16:9 Thumbnails (`P8.1`):**
  - `data/projects/v71-accept-kaggle/chapters/1/thumbnail.jpg`: `1280x720` JPEG (`116,816 B`)
  - `output/v71-accept-kaggle/feature_thumbnail.jpg`: `1280x720` JPEG (`105,142 B`)

---

## 2. Human Approval & Exact Byte Reuse Verification (`APR-01..APR-06`)

- **`APR-01` (Pre-Approval Block):** `produce -p v71-accept-local -c 1 --json` and `produce -p v71-accept-kaggle -c 1 --json` both failed closed (`exit_code=2`) prior to human approval.
- **`APR-02` (Explicit Human Approval):** Both Chapter 1 previews were explicitly approved by `reviewer="user"` after presentation of real artifacts and review packs:
  - `v71-accept-local`: `config_fingerprint=9de3eee1fff96c9c54ebd078ba6c3957b10874c9952f5de22775643a31617d18`, `artifact_hash=899c5782bf418df0c33ea3a7240af49e2924636fcf5531bcd25a71d704b58ebd`
  - `v71-accept-kaggle`: `config_fingerprint=dd2d83fece63bb5ff19419895adea66489b81c67f5c1a46755e02f9676bd3141`, `artifact_hash=3cd557849c9ddcb828aa41e901cd42c4929ab5fe4c3a2436e7ad9753f350948d`
- **`APR-03 / APR-04 / APR-05` (Stale Approval Invalidation on Disposable Copies):** Mutating `script.json`, appending 1 byte to `preview.mp4`, or changing `voice_selection.json` all caused `require_preview_approval()` to reject with `DomainValidationError` (`Gate G6 rejected`).
- **`APR-06` (Exact Chapter 1 Byte Promotion):**
  - Local: `SHA256(preview.mp4) == SHA256(chapters/1/video/final.mp4) == SHA256(output/v71-accept-local/chapter_001.mp4)` (`899c5782bf418df0...`, promoted in `0.073s`).
  - Kaggle: `SHA256(preview.mp4) == SHA256(chapters/1/video/final.mp4) == SHA256(output/v71-accept-kaggle/chapter_001.mp4)` (`3cd557849c9ddcb8...`).

---

## 3. Hardware Coverage Note

- Local macOS Apple Silicon host has `libx264` and `h264_videotoolbox` (verified available via `ffmpeg -encoders`); no local NVIDIA `h264_nvenc` hardware exists locally (NVENC verified live on Kaggle Dual-T4).
