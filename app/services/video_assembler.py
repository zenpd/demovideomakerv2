"""
Video Assembler – live browser recordings + TTS audio → polished MP4 via FFmpeg.

Pipeline per scene:
  1. Trim/pad the recorded .webm to exactly match TTS audio duration
  2. Replace browser's silent audio track with TTS narration
  3. Burn rich lower-third overlay (title + caption + progress bar) via FFmpeg drawtext
  4. Concat all scene clips + optional animated title card → final MP4

v3: Uses live Playwright video recordings instead of static screenshots.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).parent.parent
_FFMPEG  = str(_APP_DIR / "bin" / "ffmpeg")  if (_APP_DIR / "bin" / "ffmpeg").exists()  else "ffmpeg"
_FFPROBE = str(_APP_DIR / "bin" / "ffprobe") if (_APP_DIR / "bin" / "ffprobe").exists() else "ffprobe"

_FPS = 24

# Colour palette (hex for FFmpeg drawtext)
_DARK_BG  = "0x080e28"
_ACCENT   = "0x3882f6"
_ACCENT2  = "0x8b5cf6"
_TEXT_HI  = "0xf0f5ff"
_TEXT_LO  = "0xa0b4dc"

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]
_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
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

        # Optional animated intro title card
        if demo_title:
            intro = os.path.join(work_dir, "clip_intro.mp4")
            ok = await loop.run_in_executor(None, _title_card, demo_title, intro, w, h, self.fps)
            if ok and os.path.exists(intro):
                clip_paths.append(intro)

        # Per-scene clips: trim video + replace audio + burn overlay
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
            return False

        return await loop.run_in_executor(
            None, _concat_reencode, clip_paths, output_path, work_dir, w, h, self.fps
        )


# ── Scene clip ────────────────────────────────────────────────────────────────

def _scene_clip(
    scene: Dict, output: str, w: int, h: int, fps: int, idx: int, total: int
) -> bool:
    video = scene.get("video_path", "")
    audio = scene.get("audio_path", "")
    dur   = float(scene.get("duration", 5.0))
    title = scene.get("title", f"Scene {idx + 1}")
    narration = scene.get("narration", "")

    if not audio or not os.path.exists(audio):
        logger.error("Missing audio for scene %d: %s", idx, audio)
        return False

    caption_lines = _wrap_caption(narration, 80)
    cap_fs   = max(13, h // 52)
    title_fs = max(18, h // 34)
    band_h   = title_fs + (cap_fs + 4) * max(len(caption_lines), 1) + 28
    bar_y    = h - band_h - 6
    text_y   = h - band_h + 10
    cap_y    = h - band_h + 12 + title_fs
    pb_w     = max(1, int(w * (idx + 1) / total))
    badge    = f"{idx + 1}/{total}"

    font      = _find_font(bold=False)
    font_bold = _find_font(bold=True)
    font_arg      = f":fontfile={font}"      if font      else ""
    font_bold_arg = f":fontfile={font_bold}" if font_bold else ""

    # ── Pass 1: Combine video + audio into intermediate MP4 ──────────────────
    pass1 = output + ".p1.mp4"
    scale_vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )

    if video and os.path.exists(video):
        cmd1 = [
            _FFMPEG, "-y",
            "-i", video,
            "-i", audio,
            "-vf", scale_vf,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", str(dur + 0.1),
            "-shortest",
            pass1,
        ]
    else:
        logger.warning("Scene %d: no video recording – using colour card", idx)
        cmd1 = [
            _FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c=0x0a0f2a:s={w}x{h}:r={fps}",
            "-i", audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", str(dur + 0.1),
            "-shortest",
            pass1,
        ]

    if not _run(cmd1, timeout=300):
        logger.error("Scene %d pass-1 (video+audio) failed", idx)
        return False

    # ── Pass 2: Burn overlay via -vf (textfile= avoids all escaping issues) ──
    title_file = output + ".title.txt"
    cap_file   = output + ".cap.txt"
    try:
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(title)
        cap_text = "  ".join(caption_lines)
        with open(cap_file, "w", encoding="utf-8") as f:
            f.write(cap_text)
    except Exception as e:
        logger.error("Could not write text temp files: %s", e)
        shutil.move(pass1, output)
        return True  # Return pass-1 without overlay rather than failing

    vf_parts = [
        # Dark band at bottom
        f"drawbox=x=0:y={h - band_h}:w={w}:h={band_h}:color=0x080c26@0.88:t=fill",
        # Top gradient accent bars
        f"drawbox=x=0:y={h - band_h}:w={w // 2}:h=4:color=0x3882f6:t=fill",
        f"drawbox=x={w // 2}:y={h - band_h}:w={w // 2}:h=4:color=0x8b5cf6:t=fill",
        # Progress bar background + fill
        f"drawbox=x=0:y={bar_y}:w={w}:h=5:color=0x141430@0.85:t=fill",
        f"drawbox=x=0:y={bar_y}:w={pb_w}:h=5:color=0x3882f6:t=fill",
        # Scene title (textfile to avoid escaping issues)
        f"drawtext=textfile={title_file}:x=18:y={text_y}"
        f":fontsize={title_fs}:fontcolor=0xf0f5ff{font_bold_arg}"
        f":shadowx=2:shadowy=2:shadowcolor=0x000000@0.6",
        # Caption (textfile)
        f"drawtext=textfile={cap_file}:x=18:y={cap_y}"
        f":fontsize={cap_fs}:fontcolor=0xa0b4dc{font_arg}",
        # Scene badge top-right
        f"drawbox=x={w - 120}:y=10:w=112:h=30:color=0x080e28@0.80:t=fill",
        f"drawtext=text={badge}:x={w - 110}:y=18"
        f":fontsize={max(13, h // 50)}:fontcolor=0xf0f5ff{font_arg}",
    ]
    vf = ",".join(vf_parts)

    cmd2 = [
        _FFMPEG, "-y",
        "-i", pass1,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        output,
    ]
    ok = _run(cmd2, timeout=300)

    # Cleanup temp files
    for f in [pass1, title_file, cap_file]:
        try:
            os.unlink(f)
        except Exception:
            pass

    if not ok:
        logger.error("Scene %d pass-2 (overlay) failed – using pass-1 output", idx)
        # Fallback: use the pass-1 clip without overlay
        if os.path.exists(pass1):
            shutil.move(pass1, output)
            return True
        return False

    return True


# ── Title card ────────────────────────────────────────────────────────────────

def _title_card(title: str, output: str, w: int, h: int, fps: int) -> bool:
    """4-second animated title card using FFmpeg -vf drawtext with textfile."""
    font_bold = _find_font(bold=True)
    font      = _find_font(bold=False)
    font_bold_arg = f":fontfile={font_bold}" if font_bold else ""
    font_arg      = f":fontfile={font}"      if font      else ""

    title_fs  = max(52, w // 14)
    sub_fs    = max(24, w // 34)

    # Write title text to file to avoid escaping issues
    title_file   = output + ".title.txt"
    tagline_file = output + ".tagline.txt"
    try:
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(title)
        with open(tagline_file, "w", encoding="utf-8") as f:
            f.write("Demo Walkthrough")
    except Exception as e:
        logger.error("Could not write title text files: %s", e)

    # Animated overlays using expressions (t = current time in seconds)
    vf_parts = [
        # Title text: fades in over first 0.5s
        f"drawtext=textfile={title_file}:x=(w-tw)/2:y=(h-th)/2"
        f":fontsize={title_fs}:fontcolor=0xf0f5ff{font_bold_arg}"
        f":alpha='min(t*2\\,1.0)':shadowx=3:shadowy=3:shadowcolor=0x000000@0.5",
        # Tagline: delayed fade-in starting at 0.4s
        f"drawtext=textfile={tagline_file}:x=(w-tw)/2:y=(h+th)/2+24"
        f":fontsize={sub_fs}:fontcolor=0x3882f6{font_arg}"
        f":alpha='max(0\\,min((t-0.4)*2.5\\,1.0))'",
        # Bottom accent bar (grows across screen over 2s)
        f"drawbox=x=0:y={h - 6}:w='min(t/2\\,1)*{w}':h=6:color=0x8b5cf6:t=fill",
        # Top accent bar under title
        f"drawbox=x='(iw-min(t*{w}\\,{w // 2}))/2':y={(h // 2) - (title_fs // 2) - 20}"
        f":w='min(t*{w}\\,{w // 2})':h=4:color=0x3882f6:t=fill",
    ]
    vf = ",".join(vf_parts)

    cmd = [
        _FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c=0x080e28:s={w}x{h}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-pix_fmt", "yuv420p",
        "-t", "4",
        output,
    ]
    ok = _run(cmd, timeout=120)

    for f in [title_file, tagline_file]:
        try:
            os.unlink(f)
        except Exception:
            pass

    if not ok:
        logger.warning("Animated title card failed – using plain colour fallback")
        cmd2 = [
            _FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c=0x080e28:s={w}x{h}:r={fps}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", "4",
            output,
        ]
        return _run(cmd2, timeout=60)
    return True


# ── Concat ────────────────────────────────────────────────────────────────────

def _concat_reencode(
    clips: List[str], output: str, work_dir: str, w: int, h: int, fps: int
) -> bool:
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
        _FFMPEG, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output,
    ]
    return _run(cmd, timeout=600)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wrap_caption(text: str, max_chars: int = 80) -> List[str]:
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []
    return textwrap.wrap(text, width=max_chars)[:2]


def _esc(s: str) -> str:
    """Escape text for FFmpeg drawtext filter."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")


def _run(cmd: List[str], timeout: int = 120) -> bool:
    logger.debug("FFmpeg: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error("FFmpeg error: %s", r.stderr[-800:] if r.stderr else "")
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out (%ds)", timeout)
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found at: %s", _FFMPEG)
        return False


logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).parent.parent
_FFMPEG  = str(_APP_DIR / "bin" / "ffmpeg")  if (_APP_DIR / "bin" / "ffmpeg").exists()  else "ffmpeg"
_FFPROBE = str(_APP_DIR / "bin" / "ffprobe") if (_APP_DIR / "bin" / "ffprobe").exists() else "ffprobe"

_FPS   = 24
_ZOOM  = 0.0007   # Ken Burns zoom increment per frame → ~1.15× over 10 s

# Colour palette
_DARK_BG = (8,  14,  40)
_ACCENT  = (56, 130, 246)   # blue
_ACCENT2 = (139, 92, 246)   # purple
_TEXT_HI = (240, 245, 255)
_TEXT_LO = (160, 180, 220)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for p in (_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


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

        # Annotate screenshots with rich lower-third + captions
        loop = asyncio.get_event_loop()
        for scene in scenes:
            idx = scene.get("index", scenes.index(scene))
            scene["screenshots"] = await loop.run_in_executor(
                None, _annotate_scene,
                scene.get("screenshots", []),
                scene.get("title", ""),
                scene.get("narration", ""),
                idx, len(scenes), w, h,
            )

        clip_paths: List[str] = []

        # Optional intro title card
        if demo_title:
            intro = os.path.join(work_dir, "clip_intro.mp4")
            ok = await loop.run_in_executor(None, _title_card, demo_title, intro, w, h, self.fps)
            if ok and os.path.exists(intro):
                clip_paths.append(intro)

        # Per-scene clips
        for i, scene in enumerate(scenes):
            clip = os.path.join(work_dir, f"clip_{i:03d}.mp4")
            ok = await loop.run_in_executor(None, _scene_clip, scene, clip, w, h, self.fps)
            if ok and os.path.exists(clip):
                clip_paths.append(clip)
            else:
                logger.warning("Scene %d clip failed – skipping", i)

        if not clip_paths:
            return False

        # Re-encode concat for guaranteed audio
        return await loop.run_in_executor(
            None, _concat_reencode, clip_paths, output_path, work_dir, w, h, self.fps
        )


# ── Annotation ───────────────────────────────────────────────────────────────


def _annotate_scene(
    shots: List[str], title: str, narration: str,
    idx: int, total: int, w: int, h: int
) -> List[str]:
    """Rich lower-third overlay: scene title, narration caption, gradient progress bar."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return shots

    out = []
    caption_lines = _wrap_caption(narration, 88)

    for path in shots:
        if not os.path.exists(path):
            continue
        try:
            img = Image.open(path).convert("RGBA").resize((w, h), Image.LANCZOS)
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            cap_fs   = max(13, h // 52)
            title_fs = max(18, h // 34)
            cap_font   = _font(cap_fs)
            title_font = _font(title_fs, bold=True)

            n_cap  = len(caption_lines)
            band_h = title_fs + (cap_fs + 4) * max(n_cap, 1) + 28

            # Gradient dark band
            for row in range(band_h):
                alpha = int(215 * (row / band_h) ** 0.35)
                draw.rectangle(
                    [(0, h - band_h + row), (w, h - band_h + row + 1)],
                    fill=(8, 12, 38, alpha),
                )

            # Gradient accent bar at band top
            for px in range(w):
                t = px / w
                rc = int(_ACCENT[0] * (1 - t) + _ACCENT2[0] * t)
                gc = int(_ACCENT[1] * (1 - t) + _ACCENT2[1] * t)
                bc = int(_ACCENT[2] * (1 - t) + _ACCENT2[2] * t)
                draw.rectangle([(px, h - band_h), (px + 1, h - band_h + 4)],
                                fill=(rc, gc, bc, 255))

            # Scene title
            draw.text((18, h - band_h + 10), title, fill=(*_TEXT_HI, 255), font=title_font)

            # Caption (narration text)
            for li, line in enumerate(caption_lines):
                y = h - band_h + 12 + title_fs + li * (cap_fs + 4)
                draw.text((18, y), line, fill=(*_TEXT_LO, 215), font=cap_font)

            # Scene counter badge (top-right)
            badge = f"{idx + 1} / {total}"
            badge_font = _font(max(13, h // 50))
            bbox = draw.textbbox((0, 0), badge, font=badge_font)
            bw = bbox[2] - bbox[0] + 22
            bh_b = bbox[3] - bbox[1] + 12
            bx, by = w - bw - 14, 14
            draw.rounded_rectangle([bx, by, bx + bw, by + bh_b], radius=7,
                                    fill=(*_DARK_BG, 200))
            draw.rounded_rectangle([bx, by, bx + bw, by + bh_b], radius=7,
                                    outline=(*_ACCENT, 180), width=1)
            draw.text((bx + 11, by + 6), badge, fill=(*_TEXT_HI, 230), font=badge_font)

            # Gradient progress bar above band
            pb_y = h - band_h - 6
            draw.rectangle([(0, pb_y), (w, pb_y + 5)], fill=(20, 24, 60, 180))
            pb_w = int(w * (idx + 1) / total)
            for px in range(pb_w):
                t = px / max(pb_w, 1)
                rc = int(_ACCENT[0] * (1 - t) + _ACCENT2[0] * t)
                gc = int(_ACCENT[1] * (1 - t) + _ACCENT2[1] * t)
                bc = int(_ACCENT[2] * (1 - t) + _ACCENT2[2] * t)
                draw.rectangle([(px, pb_y), (px + 1, pb_y + 5)], fill=(rc, gc, bc, 235))

            combined = Image.alpha_composite(img, overlay).convert("RGB")
            combined.save(path, quality=95)
            out.append(path)
        except Exception as e:
            logger.warning("Annotation failed %s: %s", path, e)
            out.append(path)
    return out


def _wrap_caption(text: str, max_chars: int = 88) -> List[str]:
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []
    return textwrap.wrap(text, width=max_chars)[:2]




def _title_card(title: str, output: str, w: int, h: int, fps: int) -> bool:
    """Animated 4-second title card: gradient bg, fade-in title, animated accent bars."""
    try:
        from PIL import Image, ImageDraw
        frames_dir = output + "_frames"
        os.makedirs(frames_dir, exist_ok=True)
        n_frames = fps * 4
        title_font = _font(max(52, w // 14), bold=True)
        sub_font   = _font(max(24, w // 34))
        tagline    = "Demo Walkthrough"

        for fi in range(n_frames):
            t = fi / n_frames
            img  = Image.new("RGB", (w, h), _DARK_BG)
            draw = ImageDraw.Draw(img)

            # Vertical gradient bg
            for y in range(h):
                yt = y / h
                r = int(_DARK_BG[0] + 12 * yt)
                g = int(_DARK_BG[1] + 8  * yt)
                b = int(_DARK_BG[2] + 30 * yt)
                draw.rectangle([(0, y), (w, y + 1)], fill=(r, g, b))

            # Subtle grid
            for gx in range(0, w, w // 14):
                draw.line([(gx, 0), (gx, h)], fill=(60, 80, 160, 22), width=1)
            for gy in range(0, h, h // 9):
                draw.line([(0, gy), (w, gy)], fill=(60, 80, 160, 22), width=1)

            # Animated gradient accent line above title
            line_w = int(w * 0.5 * min(t * 3, 1.0))
            bbox0  = draw.textbbox((0, 0), title, font=title_font)
            th_val = bbox0[3] - bbox0[1]
            ly = h // 2 - th_val // 2 - 24
            lx = (w - line_w) // 2
            for px in range(line_w):
                pt = px / max(line_w, 1)
                rc = int(_ACCENT[0] * (1 - pt) + _ACCENT2[0] * pt)
                gc = int(_ACCENT[1] * (1 - pt) + _ACCENT2[1] * pt)
                bc = int(_ACCENT[2] * (1 - pt) + _ACCENT2[2] * pt)
                draw.rectangle([(lx + px, ly), (lx + px + 1, ly + 5)], fill=(rc, gc, bc))

            # Title text (fade in)
            fade = min(1.0, t * 2.8)
            tw_val = bbox0[2] - bbox0[0]
            tx = (w - tw_val) // 2
            ty = h // 2 - th_val // 2
            fa = int(255 * fade)
            draw.text((tx + 3, ty + 3), title, fill=(0, 0, 20, fa), font=title_font)
            draw.text((tx, ty), title, fill=(*_TEXT_HI, fa), font=title_font)

            # Tagline (delayed fade in)
            fade2 = min(1.0, max(0.0, (t - 0.35) * 3))
            bbox2 = draw.textbbox((0, 0), tagline, font=sub_font)
            tw2 = bbox2[2] - bbox2[0]
            draw.text(((w - tw2) // 2, ty + th_val + 20), tagline,
                      fill=(*_ACCENT, int(210 * fade2)), font=sub_font)

            # Bottom accent bar (animates width)
            bpw = int(w * min(t * 2, 1.0))
            for px in range(bpw):
                pt = px / max(bpw, 1)
                rc = int(_ACCENT[0] * (1 - pt) + _ACCENT2[0] * pt)
                gc = int(_ACCENT[1] * (1 - pt) + _ACCENT2[1] * pt)
                bc = int(_ACCENT[2] * (1 - pt) + _ACCENT2[2] * pt)
                draw.rectangle([(px, h - 6), (px + 1, h)], fill=(rc, gc, bc))

            img.save(os.path.join(frames_dir, f"frame_{fi:05d}.png"))

        cmd = [
            _FFMPEG, "-y",
            "-framerate", str(fps),
            "-i", os.path.join(frames_dir, "frame_%05d.png"),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-t", "4",
            output,
        ]
        ok = _run(cmd, timeout=120)
        shutil.rmtree(frames_dir, ignore_errors=True)
        return ok
    except Exception as e:
        logger.error("Title card error: %s", e)
        shutil.rmtree(output + "_frames", ignore_errors=True)
        # Fallback: plain colour card with silence
        cmd = [
            _FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c=0x080e28:s={w}x{h}:r={fps}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "4",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-pix_fmt", "yuv420p",
            output,
        ]
        return _run(cmd, timeout=60)


# ── Scene clip ───────────────────────────────────────────────────────────────

def _scene_clip(scene: Dict, output: str, w: int, h: int, fps: int) -> bool:
    shots = [p for p in scene.get("screenshots", []) if os.path.exists(p)]
    audio = scene.get("audio_path", "")
    dur   = float(scene.get("duration", 5.0))

    if not shots:
        logger.error("No screenshots for scene %s", scene.get("index"))
        return False
    if not audio or not os.path.exists(audio):
        logger.error("Missing audio for scene %s: %s", scene.get("index"), audio)
        return False

    if len(shots) == 1:
        return _ken_burns(shots[0], audio, output, w, h, fps, dur)
    return _slideshow(shots, audio, output, w, h, fps, dur)


def _ken_burns(img: str, audio: str, output: str, w: int, h: int, fps: int, dur: float) -> bool:
    """Ken Burns zoom-pan with aresample to normalise audio sample rate."""
    frames = max(int(dur * fps), fps)
    zp = (f"zoompan=z='min(zoom+{_ZOOM},1.15)':d={frames}"
          f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}")
    cmd = [
        _FFMPEG, "-y",
        "-loop", "1", "-i", img,
        "-i", audio,
        "-filter_complex",
        f"[0:v]{zp},scale={w}:{h},setsar=1[v];[1:a]aresample=44100[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-pix_fmt", "yuv420p",
        "-t", str(dur + 0.15),
        "-shortest",
        output,
    ]
    return _run(cmd, timeout=240)


def _slideshow(images: List[str], audio: str, output: str, w: int, h: int, fps: int, dur: float) -> bool:
    td = os.path.dirname(output)
    # Build concat input file
    per = dur / len(images)
    concat_file = os.path.join(td, os.path.basename(output) + ".txt")
    with open(concat_file, "w") as f:
        for img in images:
            f.write(f"file '{img}'\nduration {per:.3f}\n")
        f.write(f"file '{images[-1]}'\n")  # needed by concat demuxer

    cmd = [
        _FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-i", audio,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", str(dur),
        "-shortest",
        output,
    ]
    ok = _run(cmd, timeout=180)
    try:
        os.unlink(concat_file)
    except Exception:
        pass
    return ok


# ── Concatenate (re-encode via filter_complex for full audio) ─────────────────

def _concat_reencode(
    clips: List[str], output: str, work_dir: str, w: int, h: int, fps: int
) -> bool:
    """
    Concatenate clips using FFmpeg filter_complex concat.
    This guarantees audio continuity even when clips have different
    stream configs (e.g. silent title card + voiced scene clips).
    """
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
        _FFMPEG, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output,
    ]
    return _run(cmd, timeout=600)


# ── Runner ────────────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 120) -> bool:
    logger.debug("FFmpeg: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error("FFmpeg error: %s", r.stderr[-600:] if r.stderr else "")
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out (%ds)", timeout)
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found at: %s", _FFMPEG)
        return False
