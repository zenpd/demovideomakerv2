# Demo Video Maker

Automated browser recording and video generation system with synchronized narration.

## What's Working

- **Recording & Navigation**: Records browser interactions with cursor overlay
- **Navigation Clicks**: Clicks on navigation items (sidebar, menu, text-based nav)
- **Text-to-Speech**: Synchronized audio narration
- **Parallel Processing**: 3-concurrent recordings + parallelized TTS

## Quick Start

1. **Build & Run**
   ```bash
   podman-compose up --build -d
   ```

2. **Access Web UI**
   - http://localhost:8899

3. **Edit Script**
   - Update `script.md` with scenes
   - Supported actions: `navigate`, `click` (nav items), `scroll`, `wait_for`

### Example Script

```yaml
---
title: Demo
---

## Main Page
[url: http://localhost:5173]
[action: navigate]
[duration: 15]
Welcome page with overview.

## Analytics Menu
[url: http://localhost:5173]
[action: click]
[target: text=Analytics]
[duration: 20]
Dashboard showing metrics.
```

## Known Limitations

❌ **Cannot click semantic buttons** (e.g., "Orchestrate Payment", "Approve Transaction")  
❌ **CSS selector-based only** — no visual/intent recognition  
❌ **No complex workflows** — limited to nav clicks and simple interactions

## Need to Perform Complex Actions

**To add semantic button clicks and intent-based actions:**

Add **Stagehand integration**:
1. Install LLM deps: `openai`, `anthropic`
2. Set credentials: `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`
3. Create `app/services/stagehand_bridge.py` with vision LLM layer
4. Enable in `browser_capture.py` to use LLM for element detection

This enables: visual button recognition, semantic instructions, complex multi-step workflows.

## Architecture

```
app/
├── main.py                    # FastAPI + job pipeline
├── services/
│   ├── browser_capture.py     # Playwright recording
│   ├── script_parser.py       # Script parsing
│   ├── tts_service.py         # Text-to-speech
│   └── video_assembler.py     # FFmpeg output
└── requirements.txt
```

## API

**POST `/generate`** — Generate video from script  
**GET `/status/<job_id>`** — Check progress  
**GET `/download/<job_id>`** — Download video




API accepts script + config and creates a background job.
Script is parsed into scenes (raw text, markdown directives, or YAML frontmatter).
TTS is generated for all scenes in parallel and each scene gets final duration.
Total duration is capped to max allowed duration.
Scenes are recorded in Playwright (up to 3 concurrent), with visual cursor/ripple/spotlight overlays and inferred actions.
FFmpeg assembles per-scene clips with subtitles/lower-third, optional title card, then concatenates to final MP4.
Frontend polls job status and exposes preview/download when complete.