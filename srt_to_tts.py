#!/usr/bin/env python3
"""
SRT → TTS Timeline-Aligned Audio Generator

Parse SRT subtitle files and generate a single MP3 audio track
where each spoken segment is precisely aligned to the subtitle timestamps.

Uses:
  - CosyVoice v3-flash (DashScope API) with longyingmu_v3 voice
  - ffmpeg for audio assembly and alignment
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass

import dashscope
from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
MODEL = "cosyvoice-v3-flash"
VOICE = "longyingmu_v3"
AUDIO_FORMAT = AudioFormat.MP3_22050HZ_MONO_256KBPS

SRT_PATH = "/Users/wangyw/Downloads/subtitles.srt"
OUTPUT_PATH = "/Users/wangyw/Downloads/subtitles_voiceover.mp3"

TTS_DELAY_SECONDS = 0.5
MAX_TEXT_LENGTH = 2000
SAMPLE_RATE = 22050
FFMPEG_LOG_LEVEL = "error"

TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


# ── Data Types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubtitleEntry:
    index: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class AudioPiece:
    """A speech segment positioned at a specific time in the assembly timeline."""

    path: str
    start_ms: int
    is_silence: bool


# ── SRT Parser ───────────────────────────────────────────────────────────────


def parse_timestamp_to_ms(ts: str) -> int:
    """Convert 'HH:MM:SS,mmm' to total milliseconds."""
    match = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", ts.strip())
    if not match:
        raise ValueError(f"Invalid timestamp format: {ts!r}")
    h, m, s, ms = (int(g) for g in match.groups())
    return h * 3_600_000 + m * 60_000 + s * 1_000 + ms


def format_ms_to_time(ms: int) -> str:
    """Convert milliseconds to 'MM:SS.mmm' display string."""
    total_sec = ms / 1000.0
    minutes = int(total_sec // 60)
    seconds = total_sec % 60
    return f"{minutes:02d}:{seconds:06.3f}"


def parse_srt(file_path: str) -> list[SubtitleEntry]:
    """Parse an SRT subtitle file into a list of SubtitleEntry objects."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        return []

    entries: list[SubtitleEntry] = []
    blocks = re.split(r"\n\s*\n", content)

    for block in blocks:
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if len(lines) < 3:
            continue

        index_line, timestamp_line = lines[0], lines[1]
        if not index_line.isdigit():
            continue

        match = TIMESTAMP_RE.match(timestamp_line)
        if not match:
            continue

        groups = [int(g) for g in match.groups()]
        start_ms = groups[0] * 3_600_000 + groups[1] * 60_000 + groups[2] * 1_000 + groups[3]
        end_ms = groups[4] * 3_600_000 + groups[5] * 60_000 + groups[6] * 1_000 + groups[7]
        text = " ".join(lines[2:])

        entries.append(
            SubtitleEntry(index=int(index_line), start_ms=start_ms, end_ms=end_ms, text=text)
        )

    return entries


# ── TTS Engine ───────────────────────────────────────────────────────────────


def _patch_ws_connect(synthesizer: SpeechSynthesizer) -> None:
    """Patch WebSocket connect to bypass SSL certificate verification."""
    from dashscope.audio.tts_v2.speech_synthesizer import websocket as ws_module

    def patched_connect(timeout_seconds: int = 30) -> None:
        synthesizer.ws = ws_module.WebSocketApp(
            synthesizer.url,
            header=synthesizer.request.get_websocket_headers(
                headers=synthesizer.headers, workspace=synthesizer.workspace
            ),
            on_message=synthesizer.on_message,
            on_error=synthesizer.on_error,
            on_close=synthesizer.on_close,
        )
        synthesizer.thread = threading.Thread(
            target=synthesizer.ws.run_forever,
            kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}},
        )
        synthesizer.thread.daemon = True
        synthesizer.thread.start()

        start = time.time()
        while (
            not (synthesizer.ws.sock and synthesizer.ws.sock.connected)
            and (time.time() - start) < timeout_seconds
        ):
            time.sleep(0.1)

        if not (synthesizer.ws.sock and synthesizer.ws.sock.connected):
            raise TimeoutError("WebSocket connection timeout")

    synthesizer._SpeechSynthesizer__connect = patched_connect


def synthesize_text(text: str) -> bytes:
    """Synthesize text to audio bytes using CosyVoice v3 API."""
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"Text too long ({len(text)} > {MAX_TEXT_LENGTH} chars)")

    synthesizer = SpeechSynthesizer(
        model=MODEL,
        voice=VOICE,
        format=AUDIO_FORMAT,
    )
    _patch_ws_connect(synthesizer)

    audio = synthesizer.call(text)
    if not audio:
        resp = synthesizer.get_response()
        raise RuntimeError(f"Synthesis failed: {resp}")

    return audio


# ── Audio Utilities (ffmpeg) ─────────────────────────────────────────────────


def get_audio_duration_ms(file_path: str) -> int:
    """Get audio file duration in milliseconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            file_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)
    duration_sec = float(info["format"]["duration"])
    return int(duration_sec * 1000)


def generate_silence_wav(duration_ms: int, output_path: str) -> str:
    """Generate a silent WAV file at the configured sample rate."""
    duration_sec = max(0.001, duration_ms / 1000.0)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", FFMPEG_LOG_LEVEL,
            "-f", "lavfi", "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-t", str(duration_sec),
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            output_path,
        ],
        capture_output=True,
        check=True,
    )
    return output_path


def normalize_to_wav(input_path: str, output_path: str) -> str:
    """Convert audio to standard WAV format (22050Hz, mono, 16-bit)."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", FFMPEG_LOG_LEVEL,
            "-i", input_path,
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-sample_fmt", "s16",
            output_path,
        ],
        capture_output=True,
        check=True,
    )
    return output_path


def trim_audio(input_path: str, max_duration_ms: int, output_path: str) -> str:
    """Trim audio to maximum duration."""
    duration_sec = max_duration_ms / 1000.0
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", FFMPEG_LOG_LEVEL,
            "-i", input_path,
            "-t", str(duration_sec),
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            output_path,
        ],
        capture_output=True,
        check=True,
    )
    return output_path


# ── Timeline Assembler ───────────────────────────────────────────────────────


def assemble_timeline(pieces: list[AudioPiece], total_duration_ms: int, output_path: str) -> str:
    """
    Assemble a timeline of audio pieces into a single output file.

    Uses ffmpeg filter_complex to overlay speech segments on a silence bed
    at their exact start positions, then merges via amerge chain.
    """
    if not pieces:
        raise ValueError("No audio pieces to assemble")

    # Single piece shortcut
    if len(pieces) == 1:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", FFMPEG_LOG_LEVEL,
                "-i", pieces[0].path,
                "-codec:a", "libmp3lame", "-b:a", "192k",
                output_path,
            ],
            capture_output=True,
            check=True,
        )
        return output_path

    # Build ffmpeg inputs and filter graph
    inputs: list[str] = []
    filter_parts: list[str] = []

    # Input 0: silence bed covering the full duration
    total_sec = total_duration_ms / 1000.0
    inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono"])
    filter_parts.append(
        f"[0:a]aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=mono,"
        f"atrim=0:{total_sec}[s]"
    )

    # Inputs 1..N: speech segments with delay to their start position
    for i, piece in enumerate(pieces, start=1):
        inputs.extend(["-i", piece.path])
        delay_ms = piece.start_ms
        filter_parts.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=mono,"
            f"adelay={delay_ms}|{delay_ms}[a{i}]"
        )

    # Mix: silence bed + all delayed speech segments
    # normalize=0 preserves original volume; dropout_transition=0 avoids fading
    mix_inputs = "[s]" + "".join(f"[a{i}]" for i in range(1, len(pieces) + 1))
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(pieces) + 1}:normalize=0:dropout_transition=0[out]"
    )

    filter_complex = ";\n".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", "-v", FFMPEG_LOG_LEVEL,
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-ar", str(SAMPLE_RATE), "-ac", "1",
        "-t", str(total_sec),
        "-codec:a", "libmp3lame", "-b:a", "192k",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg assembly failed:\n{result.stderr}")

    return output_path


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("=" * 60)
    logger.info("SRT → TTS Timeline-Aligned Audio Generator")
    logger.info("=" * 60)

    # 1. Parse SRT
    logger.info("Parsing SRT: %s", SRT_PATH)
    subtitles = parse_srt(SRT_PATH)
    if not subtitles:
        logger.error("No subtitle entries found in %s", SRT_PATH)
        sys.exit(1)
    logger.info("Found %d subtitle entries", len(subtitles))

    # 2. Setup
    dashscope.api_key = API_KEY
    temp_dir = tempfile.mkdtemp(prefix="srt_tts_")

    try:
        # 3. Generate TTS audio per subtitle
        logger.info("Generating TTS audio (model=%s, voice=%s)...", MODEL, VOICE)
        synthesized: list[tuple[SubtitleEntry, str]] = []
        failed_count = 0

        for i, sub in enumerate(subtitles):
            logger.info(
                "  [%d/%d] #%d %s \"%s...\"",
                i + 1, len(subtitles), sub.index,
                format_ms_to_time(sub.start_ms), sub.text[:30],
            )

            try:
                audio_bytes = synthesize_text(sub.text)

                temp_mp3 = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
                with open(temp_mp3, "wb") as f:
                    f.write(audio_bytes)

                synthesized.append((sub, temp_mp3))
                logger.info("    ✓ %d bytes", len(audio_bytes))

            except Exception as e:
                logger.error("    ✗ Failed: %s", e)
                failed_count += 1

            if i < len(subtitles) - 1:
                time.sleep(TTS_DELAY_SECONDS)

        if not synthesized:
            logger.error("No audio segments were generated successfully")
            sys.exit(1)

        # 4. Normalize audio & check durations
        logger.info("Checking segment durations against timeline...")
        speech_pieces: list[AudioPiece] = []
        warning_count = 0

        for idx_in_list, (sub, temp_mp3) in enumerate(synthesized):
            duration_ms = get_audio_duration_ms(temp_mp3)
            window_ms = sub.duration_ms

            # Calculate available time: extend into gap before next subtitle
            if idx_in_list < len(synthesized) - 1:
                next_start_ms = synthesized[idx_in_list + 1][0].start_ms
                available_ms = next_start_ms - sub.start_ms
            else:
                last_end_ms = max(s.end_ms for s, _ in synthesized)
                available_ms = (last_end_ms + 1000) - sub.start_ms

            if duration_ms > available_ms + 200:
                overshoot_ms = duration_ms - available_ms
                logger.warning(
                    "  #%d: audio %dms > available %dms (+%dms), unavoidable trim",
                    sub.index, duration_ms, available_ms, overshoot_ms,
                )
                trimmed = os.path.join(temp_dir, f"trimmed_{sub.index:03d}.wav")
                trim_audio(temp_mp3, available_ms, trimmed)
                speech_pieces.append(AudioPiece(path=trimmed, start_ms=sub.start_ms, is_silence=False))
                warning_count += 1
            else:
                normalized = os.path.join(temp_dir, f"norm_{sub.index:03d}.wav")
                normalize_to_wav(temp_mp3, normalized)
                speech_pieces.append(AudioPiece(path=normalized, start_ms=sub.start_ms, is_silence=False))

        # 5. Build timeline and assemble
        last_entry = max(subtitles, key=lambda s: s.end_ms)
        total_duration_ms = last_entry.end_ms + 1000  # 1s padding at end

        logger.info(
            "Assembling timeline: %d segments, total %.1fs",
            len(speech_pieces), total_duration_ms / 1000.0,
        )
        assemble_timeline(speech_pieces, total_duration_ms, OUTPUT_PATH)

        # 6. Verify output
        final_duration_ms = get_audio_duration_ms(OUTPUT_PATH)
        output_size = os.path.getsize(OUTPUT_PATH)

        logger.info("=" * 60)
        logger.info("✓ Output: %s", OUTPUT_PATH)
        logger.info("✓ Duration: %.1fs (expected ~%.1fs)", final_duration_ms / 1000.0, total_duration_ms / 1000.0)
        logger.info("✓ Size: %.1f KB", output_size / 1024)
        logger.info("✓ Segments: %d ok, %d failed, %d trimmed", len(speech_pieces), failed_count, warning_count)
        logger.info("=" * 60)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
