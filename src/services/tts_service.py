import os
import re
import subprocess
import wave
from pathlib import Path
import asyncio
import piper
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.utils import format_sequence_id


VOICE_MODELS = {
    "vi_VN-vivos-x_low": {
        "onnx_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vivos/x_low/vi_VN-vivos-x_low.onnx",
        "json_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vivos/x_low/vi_VN-vivos-x_low.onnx.json",
        "filename": "vi_VN-vivos-x_low.onnx"
    },
    "vi_VN-vais1000-medium": {
        "onnx_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx",
        "json_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx.json",
        "filename": "vi_VN-vais1000-medium.onnx"
    },
    "vi_VN-25hours_single-low": {
        "onnx_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/25hours_single/low/vi_VN-25hours_single-low.onnx",
        "json_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/25hours_single/low/vi_VN-25hours_single-low.onnx.json",
        "filename": "vi_VN-25hours_single-low.onnx"
    },
    "styletts2-vi": {
        "filename": "model.pth",
        "type": "styletts2"
    },
    "styletts2-vi-female": {
        "filename": "model.pth",
        "type": "styletts2"
    },
    "xtts-vi": {
        "type": "xtts",
        "reference_wav": "models/styletts2/reference_audio/vn_1.wav"
    },
    "xtts-vi-female": {
        "type": "xtts",
        "reference_wav": "models/styletts2/reference_audio/vn_2.wav"
    }
}

class TTSService:
    def __init__(self):
        self.voices = {}
        self.models_dir = Path("models/piper")
        self.styletts2_model = None

    def _ensure_model_downloaded(self, voice_name: str) -> Path:
        if voice_name not in VOICE_MODELS:
            raise ValueError(f"Unsupported voice model: {voice_name}")
            
        info = VOICE_MODELS[voice_name]
        self.models_dir.mkdir(parents=True, exist_ok=True)
        onnx_path = self.models_dir / info["filename"]
        json_path = self.models_dir / f"{info['filename']}.json"
        
        # Download ONNX if not exists
        if not onnx_path.exists():
            logger.info(f"Downloading ONNX model for {voice_name} from {info['onnx_url']}...")
            import urllib.request
            urllib.request.urlretrieve(info["onnx_url"], onnx_path)
            logger.info(f"Downloaded ONNX successfully.")
            
        # Download JSON if not exists
        if not json_path.exists():
            logger.info(f"Downloading JSON config for {voice_name} from {info['json_url']}...")
            import urllib.request
            urllib.request.urlretrieve(info["json_url"], json_path)
            logger.info(f"Downloaded JSON successfully.")
            
        return onnx_path

    def get_voice(self, voice_name: str = None):
        """
        Lazily loads the specified Piper voice model.
        """
        if not voice_name:
            voice_name = config.tts.voice if (config and config.tts) else "xtts-vi"
            
        if voice_name not in self.voices:
            onnx_path = self._ensure_model_downloaded(voice_name)
            logger.info(f"Loading Piper voice '{voice_name}' from {onnx_path}...")
            self.voices[voice_name] = piper.PiperVoice.load(str(onnx_path))
            logger.info(f"Piper voice '{voice_name}' loaded successfully.")
        return self.voices[voice_name]

    def get_audio_duration_ms(self, audio_path: str) -> int:
        """
        Runs ffprobe to retrieve the duration of an audio file in milliseconds.
        """
        cmd = [
            "ffprobe", 
            "-v", "error", 
            "-show_entries", "format=duration", 
            "-of", "default=noprint_wrappers=1:nokey=1", 
            audio_path
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            duration_sec = float(result.stdout.strip())
            return int(duration_sec * 1000)
        except Exception as e:
            logger.error(f"Error measuring audio duration of {audio_path}: {e}")
            raise

    def parse_script_text(self, script_text: str) -> list[dict]:
        """
        Parses script text/JSON to extract panel sequences and script segments.
        Supports both legacy text markers and new JSON configuration formats.
        """
        script_text = script_text.strip()
        # Check if it is JSON
        if script_text.startswith("{") and script_text.endswith("}"):
            import json
            try:
                data = json.loads(script_text)
                
                # Support wrapped "scripts" key if present
                scripts_dict = {}
                if isinstance(data, dict):
                    if "scripts" in data:
                        scripts_dict = data["scripts"]
                    else:
                        has_seq_keys = any(str(k).replace("_", "").isdigit() for k in data.keys())
                        if has_seq_keys:
                            scripts_dict = data
                
                results = []
                for k, v in scripts_dict.items():
                    seq_str = str(k).strip()
                    match_prefix = re.match(r"^(\d+)", seq_str)
                    if not match_prefix:
                        continue
                    seq_num = int(match_prefix.group(1))
                    
                    if isinstance(v, dict):
                        results.append({
                            "sequence_id": seq_str,
                            "sequence_number": seq_num,
                            "text": v.get("text", ""),
                            "focus_x": v.get("focus_x", 50),
                            "focus_y": v.get("focus_y", 50),
                            "pan_direction": v.get("pan_direction", "horizontal"),
                            "crop_scale": v.get("crop_scale", 100)
                        })
                    else:
                        results.append({
                            "sequence_id": seq_str,
                            "sequence_number": seq_num,
                            "text": str(v),
                            "focus_x": 50,
                            "focus_y": 50,
                            "pan_direction": "horizontal",
                            "crop_scale": 100
                        })
                # Sort by sequence float-like value
                def get_sort_key(x):
                    val = x["sequence_id"].replace("_", ".")
                    try:
                        return float(val)
                    except ValueError:
                        return float(x["sequence_number"])
                results.sort(key=get_sort_key)
                return results
            except Exception as e:
                logger.error(f"Error parsing script_text as JSON: {e}. Falling back to text regex.")
                
        # Legacy text regex parsing (supporting sub-panel markers like [Khung hình 5_1])
        matches = re.finditer(r"\[Khung\s*hình\s*([^\]]+)\]:?\s*(.*?)(?=\[Khung\s*hình\s*[^\]]+\]|$)", script_text, re.DOTALL | re.IGNORECASE)
        results = []
        for match in matches:
            seq_str = match.group(1).strip()
            match_prefix = re.match(r"^(\d+)", seq_str)
            if not match_prefix:
                continue
            seq_num = int(match_prefix.group(1))
            text = match.group(2).strip()
            text = re.sub(r"^:\s*", "", text)
            results.append({
                "sequence_id": seq_str,
                "sequence_number": seq_num,
                "text": text,
                "focus_x": 50,
                "focus_y": 50,
                "pan_direction": "horizontal",
                "crop_scale": 100
            })
        
        def get_sort_key(x):
            val = x["sequence_id"].replace("_", ".")
            try:
                return float(val)
            except ValueError:
                return float(x["sequence_number"])
        results.sort(key=get_sort_key)
        return results

    async def generate_panel_audio_styletts2(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = None) -> int:
        """
        Generates audio using StyleTTS2 and returns its duration in ms.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Lazy load StyleTTS2 model
        if self.styletts2_model is None:
            logger.info("Initializing StyleTTS2 model...")
            import sys
            import torch
            styletts2_dir = str(Path(__file__).resolve().parent / "styletts2")
            if styletts2_dir not in sys.path:
                sys.path.append(styletts2_dir)
                
            from inference import StyleTTS2 as StyleTTS2Model
            
            config_path = "models/styletts2/config.yaml"
            models_path = "models/styletts2/model.pth"
            
            device = 'cuda' if torch.cuda.is_available() else ('mps' if torch.backends.mps.is_available() else 'cpu')
            logger.info(f"Loading StyleTTS2 on device: {device}")
            self.styletts2_model = StyleTTS2Model(config_path, models_path).eval().to(device)
            logger.info("StyleTTS2 model loaded successfully.")

        # Determine reference audio for speaker identity
        ref_audio_path = "models/styletts2/reference_audio/vn_1.wav"
        if voice_name == "styletts2-vi-female":
            ref_audio_path = "models/styletts2/reference_audio/vn_2.wav"
            
        # Run in thread pool since PyTorch / espeak-ng is CPU/GPU intensive and synchronous
        import numpy as np
        import soundfile as sf
        import torch
        
        def synthesize():
            speakers = {
                "id_1": {
                    "path": ref_audio_path,
                    "lang": "vi",
                    "speed": speed
                }
            }
            with torch.no_grad():
                styles = self.styletts2_model.get_styles(speakers, denoise=0.6, avg_style=True)
                r = self.styletts2_model.generate(text, styles, stabilize=True, n_merge=18, default_speaker="[id_1]")
                r = r / np.abs(r).max()
            sf.write(str(output_path), r, samplerate=24000)
            
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, synthesize)
        
        # Measure duration
        duration_ms = self.get_audio_duration_ms(str(output_path))
        return duration_ms

    async def generate_panel_audio_xtts(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = None) -> int:
        """
        Generates audio using XTTS and returns its duration in ms.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not hasattr(self, "xtts_model") or self.xtts_model is None:
            logger.info("Initializing XTTS model (capleaf/viXTTS)...")
            
            # Ensure model files are downloaded
            model_dir = Path("models/vixtts")
            if not model_dir.exists() or not (model_dir / "model.pth").exists():
                logger.info("Downloading capleaf/viXTTS from Hugging Face...")
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id="capleaf/viXTTS", local_dir=str(model_dir))
                logger.info("viXTTS downloaded successfully.")
            
            import torch
            from TTS.api import TTS
            
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            logger.info(f"Loading XTTS on device: {device}")
            self.xtts_model = TTS(
                model_path=str(model_dir),
                config_path=str(model_dir / "config.json"),
                gpu=(device == 'cuda')
            )
            logger.info("XTTS model loaded successfully.")

        # Determine reference audio for speaker cloning
        info = VOICE_MODELS.get(voice_name, VOICE_MODELS["xtts-vi"])
        ref_audio_path = info.get("reference_wav", "models/styletts2/reference_audio/vn_1.wav")
        
        # XTTS inference is synchronous, CPU/GPU intensive
        def synthesize():
            self.xtts_model.tts_to_file(
                text=text,
                speaker_wav=ref_audio_path,
                language="vi",
                file_path=str(output_path)
            )
            
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, synthesize)
        
        # Apply speed adjustment if speed != 1.0
        if speed != 1.0:
            temp_path = output_path.with_name(f"temp_{output_path.name}")
            output_path.rename(temp_path)
            
            # Clamp speed between 0.5 and 2.0 (FFmpeg atempo limits)
            clamped_speed = max(0.5, min(2.0, speed))
            
            cmd = [
                "ffmpeg", "-y",
                "-i", str(temp_path),
                "-filter:a", f"atempo={clamped_speed}",
                str(output_path)
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                logger.error(f"FFmpeg speed adjustment failed: {stderr.decode()}")
                if temp_path.exists():
                    temp_path.rename(output_path)
            else:
                if temp_path.exists():
                    temp_path.unlink()
                    
        # Measure duration
        duration_ms = self.get_audio_duration_ms(str(output_path))
        return duration_ms

    async def generate_panel_audio(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = None) -> int:
        """
        Generates audio using selected engine (Piper, StyleTTS2, or XTTS).
        If speed != 1.0, applies FFmpeg atempo audio filter to adjust speed.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not voice_name:
            voice_name = config.tts.voice if (config and config.tts) else "xtts-vi"

        # Route to StyleTTS2 if selected
        if voice_name.startswith("styletts2"):
            try:
                duration_ms = await self.generate_panel_audio_styletts2(text, output_path, speed, voice_name)
                return duration_ms
            except Exception as e:
                logger.error(f"Error in generate_panel_audio with StyleTTS2: {e}. Falling back to Piper...")
                voice_name = "vi_VN-vais1000-medium"
        
        # Route to XTTS if selected
        if voice_name.startswith("xtts"):
            try:
                duration_ms = await self.generate_panel_audio_xtts(text, output_path, speed, voice_name)
                return duration_ms
            except Exception as e:
                logger.error(f"Error in generate_panel_audio with XTTS: {e}. Falling back to Piper...")
                voice_name = "vi_VN-vais1000-medium"
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
            
        try:
            voice = self.get_voice(voice_name)
            
            if speed != 1.0:
                temp_path = output_path.with_name(f"temp_{output_path.name}")
                
                def synthesize():
                    with wave.open(str(temp_path), "wb") as wav_file:
                        voice.synthesize_wav(text, wav_file)
                        
                await loop.run_in_executor(None, synthesize)
                
                # Clamp speed between 0.5 and 2.0 (FFmpeg atempo limits)
                clamped_speed = max(0.5, min(2.0, speed))
                
                # Use FFmpeg to adjust audio speed
                cmd = [
                    "ffmpeg", "-y",
                    "-i", str(temp_path),
                    "-filter:a", f"atempo={clamped_speed}",
                    str(output_path)
                ]
                
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                
                if proc.returncode != 0:
                    logger.error(f"FFmpeg speed adjustment failed: {stderr.decode()}")
                    # Fallback to moving temp file directly
                    if temp_path.exists():
                        temp_path.rename(output_path)
                else:
                    if temp_path.exists():
                        temp_path.unlink()
            else:
                def synthesize():
                    with wave.open(str(output_path), "wb") as wav_file:
                        voice.synthesize_wav(text, wav_file)
                        
                await loop.run_in_executor(None, synthesize)
            
            # Measure duration
            duration_ms = self.get_audio_duration_ms(str(output_path))
            return duration_ms
        except Exception as e:
            logger.error(f"Error in generate_panel_audio with Piper: {e}")
            raise

    def concatenate_audio_files(self, audio_paths: list[str], output_path: str):
        """
        Losslessly concatenates multiple WAV files using FFmpeg demuxer.
        """
        list_file_path = Path(output_path).parent / "concat_list.txt"
        with open(list_file_path, "w", encoding="utf-8") as f:
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                f.write(f"file '{abs_path}'\n")
                
        cmd = [
            "ffmpeg", "-y", 
            "-f", "concat", 
            "-safe", "0", 
            "-i", str(list_file_path), 
            "-c", "copy", 
            output_path
        ]
        
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            logger.info(f"Successfully concatenated WAV files into: {output_path}")
            if list_file_path.exists():
                list_file_path.unlink()
        except Exception as e:
            logger.error(f"Failed to concatenate WAV files using FFmpeg: {e}")
            raise

    async def process_script_segment(self, segment_id: str, chapter_num: str, speed: float = 1.0, voice_name: str = None, progress_callback = None) -> bool:
        """
        Processes a single script segment:
        1. Query segment script text.
        2. Parse panels and synthesize individual Piper WAV audios.
        3. Concatenate them into a segment-level WAV file.
        4. Update script_segments in Supabase.
        """
        logger.info(f"Generating TTS for Script Segment ID: {segment_id}...")
        
        try:
            res = supabase.table("script_segments").select("*").eq("id", segment_id).execute()
            if not res.data:
                logger.error(f"Script segment not found: {segment_id}")
                return False
                
            seg = res.data[0]
            script_text = seg["script_text"]
            start_seq = seg["start_panel_seq"]
            end_seq = seg["end_panel_seq"]
            
            panels_data = self.parse_script_text(script_text)
            if not panels_data:
                logger.error("No panel markers found in script text")
                return False
                
            from src.core.utils import get_project_title_by_chapter_id, get_project_data_dir
            project_title = get_project_title_by_chapter_id(seg["chapter_id"])
            project_dir = get_project_data_dir(project_title)
            audio_dir = project_dir / "audio"
            individual_paths = []
            total_duration_ms = 0
            
            for item in panels_data:
                seq_id = format_sequence_id(item.get("sequence_id") or str(item["sequence_number"]))
                text = item["text"]
                
                panel_audio_path = audio_dir / f"audio_{chapter_num}_{seq_id}.wav"
                logger.info(f"Synthesizing audio for panel {seq_id} using {voice_name or 'default voice'}...")
                
                duration_ms = await self.generate_panel_audio(text, panel_audio_path, speed, voice_name)
                logger.info(f"Panel {seq_id} audio generated. Duration: {duration_ms} ms")
                
                individual_paths.append(str(panel_audio_path))
                total_duration_ms += duration_ms
                
                if progress_callback:
                    await progress_callback(
                        "tts_panel",
                        f"Đã tạo giọng đọc cho khung hình {seq_id}.",
                        {
                            "sequence_id": seq_id,
                            "sequence_number": item["sequence_number"],
                            "duration_ms": duration_ms
                        }
                    )
                
            segment_audio_path = audio_dir / f"audio_{chapter_num}_seg_{start_seq}_{end_seq}.wav"
            self.concatenate_audio_files(individual_paths, str(segment_audio_path))
            
            supabase.table("script_segments").update({
                "audio_url": str(segment_audio_path),
                "audio_duration_ms": total_duration_ms,
                "status": "completed"
            }).eq("id", segment_id).execute()
            
            logger.info(f"Script segment {start_seq}-{end_seq} audio processing complete.")
            return True
            
        except Exception as e:
            logger.error(f"Error in process_script_segment: {e}")
            supabase.table("script_segments").update({
                "status": "failed"
            }).eq("id", segment_id).execute()
            return False

    async def process_chapter_audio(self, chapter_id: str, chapter_num: str, speed: float = 1.0, voice_name: str = None, progress_callback = None) -> bool:
        """
        Processes all script segments for a chapter and generates audios.
        """
        logger.info(f"Starting audio generation for Chapter {chapter_num} using Piper...")
        try:
            res = supabase.table("script_segments").select("*").eq("chapter_id", chapter_id).execute()
            segments = res.data
            
            if not segments:
                logger.warning("No script segments found to synthesize.")
                return False
                
            for seg in segments:
                audio_path = seg.get("audio_url")
                if seg.get("status") == "completed" and audio_path and os.path.exists(audio_path):
                    logger.info(f"Audio for segment {seg['start_panel_seq']}-{seg['end_panel_seq']} already exists. Skipping.")
                    continue
                    
                success = await self.process_script_segment(seg["id"], chapter_num, speed, voice_name, progress_callback)
                if not success:
                    return False
                    
            logger.info(f"Audio generation for Chapter {chapter_num} completed successfully.")
            return True
        except Exception as e:
            logger.error(f"Error in process_chapter_audio: {e}")
            return False

tts_service = TTSService()
