"""
Script Parser: converts plain text / Markdown / YAML-frontmatter scripts into
a list of scene dicts.

Scene dict:
  {
    "index": int,
    "title": str,
    "narration": str,
    "url": str | None,     # override app_url for this scene
    "action": str,         # "navigate" | "scroll" | "click" | "wait"
    "target": str,         # CSS selector or empty    "wait_for": str,       # CSS selector to wait for before starting action  }

Script format (simplest):
  ## Scene Title
  [action: scroll | url: https://...]
  Narration text for this scene.

Or plain paragraphs – each paragraph becomes a scene.
"""
import re
from typing import Any, Dict, List, Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


class ScriptParser:
    def parse(self, script: str, default_url: str = "") -> List[Dict[str, Any]]:
        script = script.strip()
        if not script:
            return []

        # ── YAML frontmatter? ────────────────────────────────────────
        meta: Dict[str, Any] = {}
        if script.startswith("---") and _HAS_YAML:
            end = script.find("\n---", 3)
            if end != -1:
                try:
                    meta = yaml.safe_load(script[3:end]) or {}
                    script = script[end + 4:].strip()
                except Exception:
                    pass

        demo_title = meta.get("title", "")

        # ── Try markdown heading-based parsing ──────────────────────
        scenes = self._parse_markdown(script, default_url)
        if not scenes:
            # Fallback: split on blank lines as paragraphs
            scenes = self._parse_paragraphs(script, default_url)

        if scenes and demo_title:
            scenes[0]["demo_title"] = demo_title

        return scenes

    # ── Markdown parser ──────────────────────────────────────────────

    def _parse_markdown(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        """Split on ## headings."""
        blocks = re.split(r'^#{1,3}\s+', text, flags=re.MULTILINE)
        scenes = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            title = lines[0].strip() if lines else ""
            rest = "\n".join(lines[1:]).strip()
            if not rest:
                # Possibly the first block before any heading - treat as narration
                if not title:
                    continue
                # title is actually narration content
                rest, title = title, f"Scene {len(scenes)+1}"

            meta_lines, narration_lines = [], []
            for ln in rest.splitlines():
                # Directive lines: [action: scroll] or [url: http...]
                m = re.match(r'^\[([a-zA-Z_]+):\s*(.+?)\]$', ln.strip())
                if m:
                    meta_lines.append((m.group(1).lower(), m.group(2).strip()))
                else:
                    narration_lines.append(ln)

            narration = " ".join(narration_lines).strip()
            if not narration:
                continue

            directives = dict(meta_lines)
            scene: Dict[str, Any] = {
                "index": len(scenes),
                "title": title,
                "narration": narration,
                "url": directives.get("url") or default_url or None,
                "action": directives.get("action", "navigate"),
                "target": directives.get("target", ""),
                "text":   directives.get("text", ""),
                "wait_for": directives.get("wait_for", ""),
            }
            # Optional explicit duration (seconds). Overrides TTS length in main.py.
            if "duration" in directives:
                try:
                    scene["duration_override"] = float(directives["duration"])
                except ValueError:
                    pass
            scenes.append(scene)

        return scenes

    # ── Paragraph parser ─────────────────────────────────────────────

    def _parse_paragraphs(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        """Each blank-line-separated paragraph = one scene."""
        paragraphs = re.split(r'\n{2,}', text)
        scenes = []
        for para in paragraphs:
            para = para.strip()
            if not para or len(para) < 10:
                continue
            sentences = re.split(r'(?<=[.!?])\s+', para)
            title = sentences[0][:60].rstrip(".!?") if sentences else f"Scene {len(scenes)+1}"
            scenes.append({
                "index": len(scenes),
                "title": title,
                "narration": para,
                "url": default_url or None,
                "action": "navigate",
                "target": "",
                "text": "",
            })
        return scenes
