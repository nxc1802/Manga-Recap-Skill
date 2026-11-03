import asyncio
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from src.services.tts_service import tts_service, VOICE_MODELS

async def main():
    parser = argparse.ArgumentParser(description="Generate references for Cloud API TTS voices")
    parser.add_argument(
        "--text",
        type=str,
        default="Xin chào, đây là bản thử nghiệm giọng đọc tiếng Việt của hệ thống Manga Recap.",
        help="Text to synthesize"
    )
    args = parser.parse_args()

    voice_mappings = {
        "edge-tts-female": ("edgetts", "sample_edge-tts-female.wav"),
        "edge-tts-male": ("edgetts", "sample_edge-tts-male.wav")
    }

    ref_dir = Path("tts_references")
    for voice_name, (sub_dir, file_name) in voice_mappings.items():
        target_dir = ref_dir / sub_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / file_name
        print(f"Generating reference audio for API voice: {voice_name} -> {target_file}")
        try:
            dur = await tts_service.generate_panel_audio(
                text=args.text,
                output_path=target_file,
                speed=1.0,
                voice_name=voice_name
            )
            print(f"Success ({dur} ms)")
        except Exception as e:
            print(f"Failed for {voice_name}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
