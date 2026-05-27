"""
Video Assembler – live browser recordings + TTS audio → polished MP4 via FFmpeg.

Per scene : scale browser .webm + replace audio → MP4 clip (pure video, no overlay)
Final     : concat all clips → output MP4
"""
import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).parent.parent
_FFMPEG  = str(_APP_DIR / "bin" / "ffmpeg")  if (_APP_DIR / "bin" / "ffmpeg").exists()  else "ffmpeg"
_FFPROBE = str(_APP_DIR / "bin" / "ffprobe") if (_APP_DIR / "bin" / "ffprobe").exists() else "ffprobe"

_FPS = 24

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _find_font(bold: bool = False) -> str:
    for p in (_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(p):
            return p
    return ""


class VideoAssembler:
    def __init__(self, fps: int = _FPS):
        self.fps = fps

    async def assemble(
        self,
        scenes: List[Dict[str, Any]],
        output_path: str,
        resolution: Tuple[int, int] = (1280, 720),
        demo_title: Optional[str] = None,
    ) -> bool:
        if not scenes:
            return False

        work_dir = os.path.dirname(os.path.abspath(output_path))
        w, h = resolution
        loop = asyncio.get_event_loop()
        clip_paths: List[str] = []

        # Optional title card
        if demo_title:
            intro = os.path.join(work_dir, "clip_intro.mp4")
            ok = await loop.run_in_executor(
                None, _title_card, demo_title, intro, w, h, self.fps
            )
            if ok and os.path.exists(intro):
                clip_paths.append(intro)

        # Per-scene clips
        for i, scene in enumerate(scenes):
            clip = os.path.join(work_dir, f"clip_{i:03d}.mp4")
            ok = await loop.run_in_executor(
                None, _scene_clip, scene, clip, w, h, self.fps, i, len(scenes)
            )
            if ok and os.path.exists(clip):
                clip_paths.append(clip)
            else:
                logger.warning("Scene %d clip failed – skipping", i)

        if not clip_paths:
            logger.error("No clips produced – cannot assemble video")
            return False

        return await loop.run_in_executor(
            None, _concat_clips, clip_paths, output_path, work_dir
        )


# ── Scene clip ────────────────────────────────────────────────────────────────

def _scene_clip(
    scene: Dict, output: str, w: int, h: int, fps: int, idx: int, total: int
) -> bool:
    video     = scene.get("video_path") or ""
    audio     = scene.get("audio_path") or ""
    dur       = float(scene.get("duration", 5.0))
    title     = scene.get("title") or f"Scene {idx + 1}"
    narration = scene.get("narration") or ""

    if not audio or not os.path.exists(audio):
        logger.error("Scene %d: missing audio: %s", idx, audio)
        return False

    # ── Encode: video + audio → output (no overlay) ────────────────────
    scale_vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps}"
    )

    if video and os.path.exists(video):
        cmd = [
            _FFMPEG, "-y",
            "-i", video, "-i", audio,
            "-vf", scale_vf,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", str(dur + 0.15), "-shortest",
            output,
        ]
    else:
        logger.warning("Scene %d: no browser recording – colour card fallback", idx)
        cmd = [
            _FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c=0x0a0f2a:s={w}x{h}:r={fps}",
            "-i", audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", str(dur + 0.15), "-shortest",
            output,
        ]

    if not _run(cmd, timeout=300):
        logger.error("Scene %d encoding failed", idx)
        return False

    return True


# ── Title card ────────────────────────────────────────────────────────────────

def _title_card(title: str, output: str, w: int, h: int, fps: int) -> bool:
    title_f   = output + ".title.txt"
    tagline_f = output + ".tag.txt"
    try:
        with open(title_f,   "w", encoding="utf-8") as f:
            f.write(title)
        with open(tagline_f, "w", encoding="utf-8") as f:
            f.write("Demo Walkthrough")
    except Exception as e:
        logger.warning("Title card: text file write failed: %s", e)
        return _plain_card(output, w, h, fps)

    font_bold = _find_font(bold=True)
    font      = _find_font(bold=False)
    fab = f":fontfile={font_bold}" if font_bold else ""
    fa  = f":fontfile={font}"      if font      else ""

    title_fs = max(52, w // 14)
    sub_fs   = max(24, w // 34)
    ty = (h - title_fs) // 2
    sy = ty + title_fs + 20

    vf_parts = [
        (f"drawtext=textfile={title_f}"
         f":x=(w-tw)/2:y={ty}"
         f":fontsize={title_fs}:fontcolor=0xf0f5ff{fab}"
         f":shadowx=3:shadowy=3:shadowcolor=0x000000@0.5"),
        (f"drawtext=textfile={tagline_f}"
         f":x=(w-tw)/2:y={sy}"
         f":fontsize={sub_fs}:fontcolor=0x3882f6{fa}"),
        f"drawbox=x=0:y={h - 6}:w={w}:h=6:color=0x8b5cf6:t=fill",
        f"drawbox=x={(w - w // 2) // 2}:y={ty - 14}:w={w // 2}:h=4:color=0x3882f6:t=fill",
    ]

    cmd = [
        _FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c=0x080e28:s={w}x{h}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", ",".join(vf_parts),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-pix_fmt", "yuv420p", "-t", "4",
        output,
    ]
    ok = _run(cmd, timeout=120)
    for f in [title_f, tagline_f]:
        try:
            os.unlink(f)
        except Exception:
            pass
    return ok if ok else _plain_card(output, w, h, fps)


def _plain_card(output: str, w: int, h: int, fps: int) -> bool:
    cmd = [
        _FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c=0x080e28:s={w}x{h}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-pix_fmt", "yuv420p", "-t", "4",
        output,
    ]
    return _run(cmd, timeout=60)


# ── Concatenate clips ─────────────────────────────────────────────────────────

def _concat_clips(clips: List[str], output: str, work_dir: str) -> bool:
    if len(clips) == 1:
        shutil.copy2(clips[0], output)
        return True
    n = len(clips)
    inputs: List[str] = []
    for c in clips:
        inputs += ["-i", c]
    filter_complex = (
        "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
        + f"concat=n={n}:v=1:a=1[vout][aout]"
    )
    cmd = [
        _FFMPEG, "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        output,
    ]
    return _run(cmd, timeout=600)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 120) -> bool:
    logger.debug("FFmpeg: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error("FFmpeg failed (rc=%d): %s", r.returncode, (r.stderr or "")[-1000:])
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out (%ds)", timeout)
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found: %s", cmd[0])
        return False
