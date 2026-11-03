import os
import re
import subprocess
import wave
import json
import base64
from pathlib import Path
import asyncio
import httpx
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.utils import format_sequence_id

VOICE_MODELS = {
    # Microsoft Edge-TTS Cloud API (Free, high-speed, neural speech, zero local models)
    "edge-tts-female": {
        "type": "edgetts",
        "voice": "vi-VN-HoaiMyNeural",
        "description": "Giọng nữ tiếng Việt (Hoài My - Microsoft Neural API)"
    },
    "edge-tts-male": {
        "type": "edgetts",
        "voice": "vi-VN-NamMinhNeural",
        "description": "Giọng nam tiếng Việt (Nam Minh - Microsoft Neural API)"
    },
    "edge-tts-en-female": {
        "type": "edgetts",
        "voice": "en-US-JennyNeural",
        "description": "Giọng nữ tiếng Anh (Jenny - Microsoft Neural API)"
    },
    "edge-tts-en-male": {
        "type": "edgetts",
        "voice": "en-US-GuyNeural",
        "description": "Giọng nam tiếng Anh (Guy - Microsoft Neural API)"
    },

    # Google Gemini TTS REST API
    "gemini-2.5-flash": {
        "type": "gemini",
        "model": "gemini-2.5-flash-preview-tts",
        "voice": "Puck",
        "description": "Giọng Gemini 2.5 Flash Audio API (Puck)"
    },
    "gemini-3.1-flash": {
        "type": "gemini",
        "model": "gemini-3.1-flash-tts-preview",
        "voice": "Puck",
        "description": "Giọng Gemini 3.1 Flash Audio API (Puck)"
    },
    "gemini-kore": {
        "type": "gemini",
        "model": "gemini-2.5-flash-preview-tts",
        "voice": "Kore",
        "description": "Giọng Gemini 2.5 Flash Audio API (Kore - Nữ)"
    },

    # OpenAI TTS Cloud API
    "openai-tts-alloy": {
        "type": "openai",
        "voice": "alloy",
        "model": "tts-1",
        "description": "Giọng OpenAI Cloud TTS (Alloy)"
    },
    "openai-tts-echo": {
        "type": "openai",
        "voice": "echo",
        "model": "tts-1",
        "description": "Giọng OpenAI Cloud TTS (Echo)"
    },
    "openai-tts-nova": {
        "type": "openai",
        "voice": "nova",
        "model": "tts-1",
        "description": "Giọng OpenAI Cloud TTS (Nova - Nữ)"
    },
    "openai-tts-shimmer": {
        "type": "openai",
        "voice": "shimmer",
        "model": "tts-1",
        "description": "Giọng OpenAI Cloud TTS (Shimmer - Nữ)"
    }
}

# Redirect legacy local voice models to cloud API voices
VOICE_ALIASES = {
    "vi_VN-vais1000-medium": "edge-tts-male",
    "vi_VN-vivos-x_low": "edge-tts-male",
    "vi_VN-25hours_single-low": "edge-tts-female",
    "xtts-vi": "edge-tts-male",
    "xtts-vi-female": "edge-tts-female",
    "vixtts-v2-male": "edge-tts-male",
    "vixtts-v2-female": "edge-tts-female",
    "vieneu-v3-male": "edge-tts-male",
    "vieneu-v3-female": "edge-tts-female",
    "valtec-male": "edge-tts-male",
    "valtec-female": "edge-tts-female",
    "valtec-sf": "edge-tts-female",
    "valtec-sm": "edge-tts-male",
    "valtec-nm2": "edge-tts-male",
    "styletts2-vi": "edge-tts-male",
    "styletts2-vi-female": "edge-tts-female",
    "vits-mms-vi": "edge-tts-female",
}

class TTSService:
    def __init__(self):
        pass

    def resolve_voice_name(self, voice_name: str = None) -> str:
        """
        Resolves voice name to an active API voice.
        Maps deprecated local voices to Edge-TTS API equivalents.
        """
        if not voice_name:
            voice_name = config.tts.voice if (config and config.tts) else "edge-tts-female"

        if voice_name in VOICE_ALIASES:
            target_api_voice = VOICE_ALIASES[voice_name]
            logger.info(f"TTS: Local voice '{voice_name}' is deprecated. Redirecting to API voice '{target_api_voice}'.")
            return target_api_voice

        if voice_name in VOICE_MODELS:
            return voice_name

        logger.warning(f"TTS: Unknown voice '{voice_name}'. Defaulting to 'edge-tts-female'.")
        return "edge-tts-female"

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
        if script_text.startswith("{") and script_text.endswith("}"):
            try:
                data = json.loads(script_text)
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
                        tag = v.get("status") or v.get("tag") or ""
                        status = "processed"
                        if tag in ("context_only", "useless", "video_and_context"):
                            status = "context_only" if tag == "context_only" else ("useless" if tag == "useless" else "processed")
                        results.append({
                            "sequence_id": seq_str,
                            "sequence_number": seq_num,
                            "text": v.get("text", ""),
                            "status": status,
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
                            "status": "processed",
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
            except Exception as e:
                logger.error(f"Error parsing script_text as JSON: {e}. Falling back to text regex.")

        # Legacy text regex parsing
        matches = re.finditer(
            r"\[Khung\s*hình\s*([^\]]+)\](?:\s*\[(context_only|video_and_context|useless)\])?:?\s*(.*?)(?=\[Khung\s*hình\s*[^\]]+\]|$)",
            script_text,
            re.DOTALL | re.IGNORECASE
        )
        results = []
        for match in matches:
            seq_str = match.group(1).strip()
            tag = (match.group(2) or "").strip().lower()
            text = match.group(3).strip()
            text = re.sub(r"^:\s*", "", text)
            
            status = "processed"
            if tag == "context_only":
                status = "context_only"
            elif tag == "useless":
                status = "useless"
                
            match_prefix = re.match(r"^(\d+)", seq_str)
            if not match_prefix:
                continue
            seq_num = int(match_prefix.group(1))
            results.append({
                "sequence_id": seq_str,
                "sequence_number": seq_num,
                "text": text,
                "status": status,
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

    async def _adjust_speed(self, output_path: Path, speed: float):
        """
        Adjusts audio file speed using FFmpeg atempo.
        """
        temp_path = output_path.with_name(f"temp_{output_path.name}")
        output_path.rename(temp_path)
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

    async def generate_panel_audio_edgetts(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = "edge-tts-female") -> int:
        """
        Generates audio using Microsoft Edge-TTS Cloud API and returns its duration in ms.
        Includes automatic retry for transient network issues.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        info = VOICE_MODELS.get(voice_name, VOICE_MODELS["edge-tts-female"])
        voice = info.get("voice", "vi-VN-HoaiMyNeural")
        
        rate_percent = int((speed - 1.0) * 100)
        rate_str = f"{rate_percent:+}%" if rate_percent != 0 else "+0%"
        
        import edge_tts
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                temp_mp3 = output_path.with_suffix(".mp3")
                await communicate.save(str(temp_mp3))
                
                # Convert MP3 to PCM WAV and strip silence
                cmd = [
                    "ffmpeg", "-y",
                    "-i", str(temp_mp3),
                    "-af", "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-50dB:stop_silence=0.25",
                    "-c:a", "pcm_s16le",
                    str(output_path)
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                if temp_mp3.exists():
                    temp_mp3.unlink()
                    
                return self.get_audio_duration_ms(str(output_path))
            except Exception as e:
                logger.warning(f"Edge-TTS API attempt {attempt} failed: {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1.0)

    async def generate_panel_audio_gemini(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = "gemini-2.5-flash") -> int:
        """
        Generates audio using Google Gemini TTS REST API and returns duration in ms.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is not configured in .env for Gemini TTS")
            
        info = VOICE_MODELS.get(voice_name, {})
        model = info.get("model", "gemini-2.5-flash-preview-tts")
        voice_preset = info.get("voice", "Puck")
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [
                {
                    "parts": [{"text": text}]
                }
            ],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice_preset
                        }
                    }
                }
            }
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.error(f"Gemini API Error: {response.text}")
                raise RuntimeError(f"Gemini API call failed with status {response.status_code}: {response.text}")
                
            data = response.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
                audio_data = None
                for part in parts:
                    if "inlineData" in part:
                        audio_data = part["inlineData"]["data"]
                        break
                        
                if not audio_data:
                    raise ValueError("No audio inlineData found in Gemini response")
                    
                raw_bytes = base64.b64decode(audio_data)
                with wave.open(str(output_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(24000)
                    wav_file.writeframes(raw_bytes)
            except Exception as e:
                logger.error(f"Failed to parse Gemini API response: {e}")
                raise RuntimeError(f"Failed to parse Gemini response: {e}")
                
        if speed != 1.0:
            await self._adjust_speed(output_path, speed)
            
        return self.get_audio_duration_ms(str(output_path))

    async def generate_panel_audio_openai(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = "openai-tts-alloy") -> int:
        """
        Generates audio using OpenAI TTS API (or compatible API endpoint).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY or OPENROUTER_API_KEY is required for OpenAI TTS")
            
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        url = f"{base_url.rstrip('/')}/audio/speech"
        
        info = VOICE_MODELS.get(voice_name, {})
        voice = info.get("voice", "alloy")
        model = info.get("model", "tts-1")
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "speed": max(0.25, min(4.0, speed)),
            "response_format": "wav"
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI TTS API call failed: {resp.status_code} - {resp.text}")
                
            with open(output_path, "wb") as f:
                f.write(resp.content)
                
        return self.get_audio_duration_ms(str(output_path))

    async def generate_panel_audio(self, text: str, output_path: Path, speed: float = 1.0, voice_name: str = None) -> int:
        """
        Generates audio using 100% Cloud API mechanisms (Edge-TTS, Gemini, OpenAI).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_voice = self.resolve_voice_name(voice_name)
        info = VOICE_MODELS.get(resolved_voice, VOICE_MODELS["edge-tts-female"])
        voice_type = info.get("type", "edgetts")

        # 1. Edge-TTS API
        if voice_type == "edgetts":
            return await self.generate_panel_audio_edgetts(text, output_path, speed, resolved_voice)

        # 2. Gemini TTS API
        elif voice_type == "gemini":
            try:
                return await self.generate_panel_audio_gemini(text, output_path, speed, resolved_voice)
            except Exception as e:
                logger.error(f"Gemini TTS failed: {e}. Falling back to Edge-TTS API...")
                return await self.generate_panel_audio_edgetts(text, output_path, speed, "edge-tts-female")

        # 3. OpenAI TTS API
        elif voice_type == "openai":
            try:
                return await self.generate_panel_audio_openai(text, output_path, speed, resolved_voice)
            except Exception as e:
                logger.error(f"OpenAI TTS failed: {e}. Falling back to Edge-TTS API...")
                return await self.generate_panel_audio_edgetts(text, output_path, speed, "edge-tts-female")

        # Fallback default
        return await self.generate_panel_audio_edgetts(text, output_path, speed, "edge-tts-female")

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
        - If voice is edge-tts: Uses unified audio generation + boundary mapping + FFmpeg slicing for high coherence.
        - Otherwise: Uses parallel API panel synthesis.
        """
        resolved_voice = self.resolve_voice_name(voice_name)
        logger.info(f"Generating Cloud API TTS for Script Segment ID: {segment_id} (Voice: {resolved_voice})...")
        
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
            audio_dir = project_dir / "audio" / "clips"
            audio_dir.mkdir(parents=True, exist_ok=True)
            
            segment_audio_path = audio_dir / f"audio_{chapter_num}_seg_{start_seq}_{end_seq}.wav"
            
            if resolved_voice.startswith("edge-tts"):
                logger.info("Using Edge-TTS API unified synthesis + boundary slicing...")
                import edge_tts
                
                info = VOICE_MODELS.get(resolved_voice, VOICE_MODELS["edge-tts-female"])
                voice = info.get("voice", "vi-VN-HoaiMyNeural")
                
                rate_percent = int((speed - 1.0) * 100)
                rate_str = f"{rate_percent:+}%" if rate_percent != 0 else "+0%"
                
                combined_text = '\n'.join(p['text'] for p in panels_data)
                temp_mp3 = audio_dir / f"temp_seg_{start_seq}_{end_seq}.mp3"
                comm = edge_tts.Communicate(combined_text, voice, rate=rate_str)
                boundaries = []
                
                with open(temp_mp3, "wb") as audio_file:
                    async for event in comm.stream():
                        if event.get("type") == "audio":
                            audio_file.write(event["data"])
                        elif event.get("type") == "SentenceBoundary":
                            boundaries.append(event)
                            
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(temp_mp3), str(segment_audio_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                )
                if temp_mp3.exists():
                    temp_mp3.unlink()
                    
                total_duration_ms = self.get_audio_duration_ms(str(segment_audio_path))
                
                panel_to_boundaries = {i: [] for i in range(len(panels_data))}
                curr_panel_idx = 0
                for b in boundaries:
                    b_text_clean = b['text'].strip().lower()
                    matched = False
                    for p_idx in range(curr_panel_idx, len(panels_data)):
                        p_text_clean = panels_data[p_idx]['text'].strip().lower()
                        b_alpha = re.sub(r'[^\w\s]', '', b_text_clean)
                        p_alpha = re.sub(r'[^\w\s]', '', p_text_clean)
                        if p_alpha and b_alpha and (b_alpha in p_alpha or p_alpha in b_alpha):
                            panel_to_boundaries[p_idx].append(b)
                            curr_panel_idx = p_idx
                            matched = True
                            break
                    if not matched:
                        panel_to_boundaries[min(curr_panel_idx, len(panels_data) - 1)].append(b)
                        
                panel_timings = []
                for i in range(len(panels_data)):
                    b_list = panel_to_boundaries[i]
                    if not b_list:
                        start_ms = panel_timings[-1]["end_ms"] if panel_timings else 0
                        duration_ms = 2500
                        end_ms = start_ms + duration_ms
                    else:
                        if i == 0:
                            start_ms = 0
                        else:
                            start_ms = b_list[0]['offset'] / 10000.0
                            
                        if i == len(panels_data) - 1:
                            end_ms = total_duration_ms
                        else:
                            next_b_list = panel_to_boundaries[i+1]
                            if next_b_list:
                                end_ms = next_b_list[0]['offset'] / 10000.0
                            else:
                                end_ms = (b_list[-1]['offset'] + b_list[-1]['duration']) / 10000.0
                                
                        duration_ms = end_ms - start_ms
                        if duration_ms <= 0:
                            duration_ms = 100
                            end_ms = start_ms + duration_ms
                            
                    panel_timings.append({
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "duration_ms": duration_ms
                    })
                    
                for i in range(len(panel_timings)):
                    item = panel_timings[i]
                    if item["start_ms"] >= total_duration_ms:
                        item["start_ms"] = total_duration_ms - 100
                    if item["end_ms"] > total_duration_ms:
                        item["end_ms"] = total_duration_ms
                    if item["end_ms"] <= item["start_ms"]:
                        item["end_ms"] = item["start_ms"] + 100
                    item["duration_ms"] = int(item["end_ms"] - item["start_ms"])
                    
                for i, item in enumerate(panels_data):
                    seq_id = format_sequence_id(item.get("sequence_id") or str(item["sequence_number"]))
                    panel_audio_path = audio_dir / f"audio_{chapter_num}_{seq_id}.wav"
                    timing = panel_timings[i]
                    
                    start_sec = timing["start_ms"] / 1000.0
                    dur_sec = timing["duration_ms"] / 1000.0
                    
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", f"{start_sec:.3f}",
                        "-t", f"{dur_sec:.3f}",
                        "-i", str(segment_audio_path),
                        "-af", "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-50dB:stop_silence=0.25",
                        "-c:a", "pcm_s16le",
                        str(panel_audio_path)
                    ]
                    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                    
                    if progress_callback:
                        await progress_callback(
                            "tts_panel",
                            f"Đã trích xuất giọng đọc API cho khung hình {seq_id} (Thời lượng: {timing['duration_ms']} ms).",
                            {
                                "sequence_id": seq_id,
                                "sequence_number": item["sequence_number"],
                                "duration_ms": timing["duration_ms"]
                            }
                        )
            else:
                logger.info(f"Synthesizing panels via Cloud API: {resolved_voice}...")
                sem = asyncio.Semaphore(5)
                
                async def synthesize_panel(item):
                    async with sem:
                        seq_id = format_sequence_id(item.get("sequence_id") or str(item["sequence_number"]))
                        text = item["text"]
                        panel_audio_path = audio_dir / f"audio_{chapter_num}_{seq_id}.wav"
                        duration_ms = await self.generate_panel_audio(text, panel_audio_path, speed, resolved_voice)
                        
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
                        return str(panel_audio_path), duration_ms

                tasks = [synthesize_panel(item) for item in panels_data]
                results = await asyncio.gather(*tasks)
                
                individual_paths = [r[0] for r in results]
                total_duration_ms = sum(r[1] for r in results)
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
        Processes all script segments for a chapter and generates audio files via API.
        """
        resolved_voice = self.resolve_voice_name(voice_name)
        logger.info(f"Starting API audio generation for Chapter {chapter_num} (Voice: {resolved_voice})...")
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
                    
                success = await self.process_script_segment(seg["id"], chapter_num, speed, resolved_voice, progress_callback)
                if not success:
                    return False
                    
            logger.info(f"Audio generation for Chapter {chapter_num} completed successfully via API.")
            return True
        except Exception as e:
            logger.error(f"Error in process_chapter_audio: {e}")
            return False

tts_service = TTSService()
