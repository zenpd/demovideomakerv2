"""
Video Assembler – screenshots + TTS audio → polished MP4 via FFmpeg.

v2 Fixes:
  • Audio concat fixed: filter_complex concat (not -c copy) → full audio
  • Title card: animated gradient, title fade-in, accent bars per-frame
  • Annotation: rich lower-third, narration captions, gradient progress bar
  • Ken Burns: aresample normalises audio before encode
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
