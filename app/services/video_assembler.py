"""
Video Assembler – live browser recordings + TTS audio → polished MP4 via FFmpeg.

Per scene : scale browser .webm + replace audio → MP4 clip (pure video, no overlay)
Final     : concat all clips → output MP4
"""
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).parent.parent
_FFMPEG  = str(_APP_DIR / "bin" / "ffmpeg")  if (_APP_DIR / "bin" / "ffmpeg").exists()  else "ffmpeg"
_FFPROBE = str(_APP_DIR / "bin" / "ffprobe") if (_APP_DIR / "bin" / "ffprobe").exists() else "ffprobe"

_FPS = 24

_FONT_CANDIDATES = [
    # Linux (Docker / Ubuntu with fonts-liberation)
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    # macOS
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _find_font(bold: bool = False) -> str:
    for p in (_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(p):
            return p
    return ""


# ── Subtitle helpers ──────────────────────────────────────────────────────────

def _write_sub(text: str, path: str, max_chars: int = 52) -> bool:
    """Wrap narration to ≤2 lines and write to subtitle text file."""
    words = text.split()
    lines: List[str] = []
    line: List[str] = []
    for word in words:
        if len(' '.join(line + [word])) > max_chars and line:
            lines.append(' '.join(line))
            line = [word]
        else:
            line.append(word)
    if line:
        lines.append(' '.join(line))
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines[:2]))
        return True
    except Exception as e:
        logger.warning("Subtitle write failed %s: %s", path, e)
        return False


def _esc_ff(s: str) -> str:
    """Escape a path/string for FFmpeg filter syntax."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _subtitle_vf(
    sub_file: str,
    title: str,
    scene_num: int,
    total_scenes: int,
    w: int,
    h: int,
    font_path: str = "",
    bold_path: str = "",
) -> str:
    """
    Build FFmpeg drawtext filter chain:
      • Dark semi-transparent lower-third band
      • Scene badge  (top-right corner)
      • Scene title  (small, accent colour, above subtitles)
      • Narration subtitle text (white, larger)
      • Thin gradient-style progress bar at very bottom
    """
    fs_sub   = max(20, w // 54)   # subtitle font size
    fs_title = max(14, w // 72)   # title label font size
    margin   = max(50, int(h * 0.082))
    band_h   = max(80, int(h * 0.175))  # lower-third height
    band_y   = h - band_h

    fp   = f":fontfile='{_esc_ff(font_path)}'"
    fb   = f":fontfile='{_esc_ff(bold_path)}'"
    sfp  = fp if font_path else ""
    sfb  = fb if bold_path else ""

    sf_esc     = _esc_ff(sub_file)
    safe_title = title[:48].replace("'", "\\'").replace(":", "\\:")
    badge_txt  = f"SCENE {scene_num}/{total_scenes}".replace("'", "\\'").replace(":", "\\:")

    title_y  = band_y + max(8, int(band_h * 0.12))
    sub_y    = title_y + fs_title + max(8, int(band_h * 0.12))

    filters = [
        # Lower-third background band
        f"drawbox=x=0:y={band_y}:w={w}:h={band_h}:color=black@0.72:t=fill",
        # Top accent line
        f"drawbox=x=0:y={band_y}:w={w}:h=3:color=0x3882f6@0.9:t=fill",
        # Progress bar at very bottom (filled proportional to scene index)
        f"drawbox=x=0:y={h-4}:w={w}:h=4:color=0x1a2035@1.0:t=fill",
        f"drawbox=x=0:y={h-4}:w={int(w * scene_num / max(total_scenes,1))}:h=4:color=0x8b5cf6@1.0:t=fill",
        # Scene title label (small, accent blue)
        f"drawtext=text='{safe_title}'"
        f":x=18:y={title_y}"
        f":fontsize={fs_title}"
        f":fontcolor=0x58a6ff@0.95"
        f"{sfb}",
        # Narration subtitle (white, with subtle shadow)
        f"drawtext=textfile='{sf_esc}'"
        f":x=(w-tw)/2"
        f":y={sub_y}"
        f":fontsize={fs_sub}"
        f":fontcolor=white@0.98"
        f":shadowx=1:shadowy=1:shadowcolor=black@0.8"
        f":line_spacing=4"
        f"{sfp}",
        # Scene badge top-right corner
        f"drawtext=text='{badge_txt}'"
        f":x=w-tw-12:y=12"
        f":fontsize={max(12, w//80)}"
        f":fontcolor=white@0.85"
        f":box=1:boxcolor=0x3882f6@0.75:boxborderw=6"
        f"{sfp}",
    ]
    return ",".join(filters)


class VideoAssembler:
    def __init__(self, fps: int = _FPS):
        self.fps = fps

    async def assemble(
        self,
        scenes: List[Dict[str, Any]],
        output_path: str,
        resolution: Tuple[int, int] = (1280, 720),
        demo_title: Optional[str] = None,
        work_dir: Optional[str] = None,
    ) -> bool:
        if not scenes:
            return False

        # Use a private temp subdirectory so parallel jobs don't stomp each other
        import tempfile, uuid as _uuid
        _own_dir = work_dir is None
        if _own_dir:
            work_dir = os.path.join(
                os.path.dirname(os.path.abspath(output_path)),
                f"_asm_{_uuid.uuid4().hex[:8]}"
            )
            os.makedirs(work_dir, exist_ok=True)
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
            if _own_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            return False

        result = await loop.run_in_executor(
            None, _concat_clips, clip_paths, output_path, work_dir
        )
        if _own_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        return result


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

    # ── Encode: video + audio → output with rich lower-third ─────────────
    scale_vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps}"
    )

    # Build rich subtitle/lower-third overlay
    sub_file = output + ".sub.txt"
    narration_text = (narration or "").strip()
    subtitle_added = False
    if narration_text and _write_sub(narration_text, sub_file):
        font = _find_font(bold=False)
        bold = _find_font(bold=True)
        sub_vf = _subtitle_vf(sub_file, title, idx + 1, total, w, h, font, bold)
        if sub_vf:
            scale_vf = scale_vf + "," + sub_vf
            subtitle_added = True

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
        # Clean up subtitle temp file on failure too
        if subtitle_added:
            try:
                os.unlink(sub_file)
            except Exception:
                pass
        return False

    # Clean up subtitle temp file
    if subtitle_added:
        try:
            os.unlink(sub_file)
        except Exception:
            pass

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
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            start_new_session=True,  # detach from parent shell's TTY/process-group
        )
        if r.returncode != 0:
            logger.error("FFmpeg failed (rc=%d): %s", r.returncode, (r.stderr or "")[-1000:])
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out (%ds)", timeout)
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found: %s", cmd[0])
        return False
