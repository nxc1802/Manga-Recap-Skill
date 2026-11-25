import asyncio
import argparse
from pathlib import Path
from src.services.tts_service import tts_service, VOICE_MODELS

async def main():
    parser = argparse.ArgumentParser(description="Test Cloud API TTS Voices")
    parser.add_argument(
        "--text", 
        type=str, 
        default="Trong bóng tối tĩnh mịch, Ghislain Perdium giật mình tỉnh giấc, đôi mắt mở to nhìn lên trần nhà.",
        help="Text to synthesize"
    )
    args = parser.parse_args()
    
    voices = [
        "edge-tts-female",
        "edge-tts-male",
        "xtts-vi"  # Deprecated voice to test graceful redirection to edge-tts-male
    ]
    
    output_dir = Path("data/tts_samples")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Synthesizing text: '{args.text}'")
    for voice in voices:
        output_path = output_dir / f"sample_{voice}.wav"
        print(f"\n--- Synthesizing with API voice: {voice} ---")
        try:
            duration_ms = await tts_service.generate_panel_audio(
                text=args.text,
                output_path=output_path,
                speed=1.0,
                voice_name=voice
            )
            print(f"Success! Output saved to: {output_path}")
            print(f"Duration: {duration_ms} ms")
        except Exception as e:
            print(f"Error synthesizing with voice {voice}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
