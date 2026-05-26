"""
Demo Video Maker – FastAPI backend
POST /api/generate  – submit job
GET  /api/status/{job_id}   – poll progress
GET  /api/download/{job_id} – download .mp4
GET  /api/voices            – list TTS voices
GET  /                      – serve UI
"""
import asyncio, logging, os, shutil, uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services.browser_capture import BrowserCapture
from services.script_parser import ScriptParser
from services.tts_service import TTSService, BUILTIN_VOICES
from services.video_assembler import VideoAssembler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).parent
TEMP_DIR   = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Demo Video Maker", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict = {}

# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(STATIC_DIR / "index.html")

@app.post("/api/generate")
async def generate(
    background_tasks: BackgroundTasks,
    script: str = Form(""),
    app_url: str = Form(...),
    voice: str = Form("en-US-AriaNeural"),
    max_duration: int = Form(240),
    resolution: str = Form("1280x720"),
    add_title: bool = Form(True),
    script_file: Optional[UploadFile] = File(None),
):
    if script_file and script_file.filename:
        raw = await script_file.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(400, "Script file must be < 5 MB")
        script = raw.decode("utf-8", errors="replace")

    script = script.strip()
    if not script:
        raise HTTPException(400, "No script provided")
    if not app_url.startswith(("http://", "https://")):
        raise HTTPException(400, "app_url must start with http:// or https://")
    if "x" not in resolution:
        raise HTTPException(400, "resolution must be WxH e.g. 1280x720")
    try:
        w, h = map(int, resolution.split("x"))
        assert 320 <= w <= 3840 and 240 <= h <= 2160
    except Exception:
        raise HTTPException(400, "Invalid resolution")

    job_id = str(uuid.uuid4())
    jobs[job_id] = _new_job()
    background_tasks.add_task(
        _run_job, job_id, script, app_url, voice,
        max(10, min(max_duration, 240)), (w, h), add_title,
    )
    logger.info("Queued job %s  url=%s", job_id, app_url)
    return JSONResponse({"job_id": job_id})

@app.get("/api/status/{job_id}")
async def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]

@app.get("/api/download/{job_id}")
async def download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(400, f"Not ready (status={job['status']})")
    mp4 = OUTPUT_DIR / f"demo_{job_id[:8]}.mp4"
    if not mp4.exists():
        raise HTTPException(500, "Output file missing")
    return FileResponse(str(mp4), media_type="video/mp4", filename=mp4.name)

@app.get("/api/voices")
async def voices():
    try:
        tts = TTSService()
        return await tts.list_voices()
    except Exception:
        return BUILTIN_VOICES

@app.delete("/api/jobs/{job_id}", include_in_schema=False)
async def delete_job(job_id: str):
    jobs.pop(job_id, None)
    job_dir = TEMP_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    mp4 = OUTPUT_DIR / f"demo_{job_id[:8]}.mp4"
    mp4.unlink(missing_ok=True)
    return {"deleted": job_id}

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Job helpers ──────────────────────────────────────────────────────────────

def _new_job() -> dict:
    return {"status": "queued", "progress": 0, "message": "Queued…",
            "output_url": None, "filename": None, "error": None,
            "total_scenes": 0, "duration_s": 0}

def _upd(job_id: str, pct: int, msg: str, **kw):
    jobs[job_id].update({"status": "processing", "progress": pct, "message": msg, **kw})
    logger.info("[%s] %3d%%  %s", job_id[:8], pct, msg)

async def _run_job(job_id, script, app_url, voice, max_duration, resolution, add_title):
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    w, h = resolution

    try:
        # 1. Parse script
        _upd(job_id, 5, "Parsing script…")
        parser = ScriptParser()
        scenes = parser.parse(script, default_url=app_url)
        demo_title = scenes[0].pop("demo_title", "") if scenes else ""
        if not scenes:
            raise ValueError("No scenes found – add narration text to your script.")
        _upd(job_id, 8, f"Found {len(scenes)} scene(s)")

        # 2. TTS narration
        _upd(job_id, 10, f"Generating voice ({voice})…")
        tts = TTSService(voice=voice)
        for i, scene in enumerate(scenes):
            audio_path = str(job_dir / f"audio_{i:03d}.mp3")
            dur = await tts.generate(scene["narration"], audio_path)
            scene["audio_path"] = audio_path
            # [duration: N] sets a minimum floor; TTS length always wins if longer
            override = scene.pop("duration_override", None)
            scene["duration"] = max(dur, override) if override else dur
            _upd(job_id, 10 + int(30 * (i + 1) / len(scenes)),
                 f"Audio {i+1}/{len(scenes)} ({scene['duration']:.1f}s)")

        # 3. Duration cap
        total_dur = sum(s["duration"] for s in scenes)
        if total_dur > max_duration:
            kept, cum = [], 0.0
            for s in scenes:
                if cum + s["duration"] <= max_duration:
                    kept.append(s); cum += s["duration"]
                else:
                    break
            scenes, total_dur = kept, cum
        jobs[job_id].update({"total_scenes": len(scenes), "duration_s": round(total_dur, 1)})
        _upd(job_id, 40, f"Narration: {total_dur:.0f}s across {len(scenes)} scenes")

        # 4. Browser recordings (live video capture)
        _upd(job_id, 42, "Launching headless browser…")
        cap = BrowserCapture(width=w, height=h)
        for i, scene in enumerate(scenes):
            video_path = await cap.capture_scene(
                url=scene.get("url") or app_url,
                action=scene.get("action", "navigate"),
                target=scene.get("target", ""),
                text=scene.get("text", ""),
                duration=scene["duration"],
                output_dir=str(job_dir),
                scene_index=i,
            )
            scene["video_path"] = video_path
            _upd(job_id, 42 + int(28 * (i + 1) / len(scenes)),
                 f"Recorded scene {i+1}/{len(scenes)}")

        # 5. Assemble video
        _upd(job_id, 72, "Assembling video (FFmpeg)…")
        out_mp4 = OUTPUT_DIR / f"demo_{job_id[:8]}.mp4"
        assembler = VideoAssembler()
        ok = await assembler.assemble(
            scenes=scenes,
            output_path=str(out_mp4),
            resolution=(w, h),
            demo_title=demo_title if add_title else None,
        )
        if not ok or not out_mp4.exists():
            raise RuntimeError("Video assembly failed – check ffmpeg logs above")

        jobs[job_id].update({
            "status": "completed", "progress": 100, "message": "Done!",
            "output_url": f"/api/download/{job_id}",
            "filename": out_mp4.name,
        })
        logger.info("[%s] Completed %s (%.1fs)", job_id[:8], out_mp4.name, total_dur)

    except Exception as exc:
        logger.exception("[%s] Job failed: %s", job_id[:8], exc)
        jobs[job_id].update({"status": "failed", "progress": 0,
                              "message": f"Error: {exc}", "error": str(exc)})
    finally:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
