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
    "target": str,         # CSS selector or empty
    "wait_for": str,       # CSS selector to wait for before starting action
  }

Script format (simplest):
  ## Scene Title
  [action: scroll | url: https://...]
  Narration text for this scene.

Or plain paragraphs – each paragraph becomes a scene.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ── Narration-to-action inference ────────────────────────────────────────────
# Patterns that detect navigation intent in narration text.
# Each pattern captures the destination label in group 1.
_NAV_PATTERNS = [
    # "let's open Analytics", "let's go to Agent Status"
    r"let['’]?s\s+(?:open|go\s+to|navigate\s+to|click\s+on|visit|view|switch\s+to)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$|\s+(?:tab|page|section|module|dashboard|panel|view))",
    # "navigate to the Analytics Dashboard"
    r"(?:navigate|go)\s+to\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$|\s+(?:tab|page|section|module|dashboard|panel|view))",
    # "open the Agent Status module" / "click the Analytics tab"
    r"(?:open|click|select|access)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\s+(?:tab|page|section|module|dashboard|panel|view)|\.|,|$)",
    # "now we'll look at Analytics"
    r"now\s+(?:we['’]?ll|let['’]?s|we\s+can)\s+(?:look\s+at|explore|examine|review|see)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$)",
]
_NAV_RE = [re.compile(p, re.IGNORECASE) for p in _NAV_PATTERNS]


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

        # ── Auto-upgrade navigate → click when narration implies navigation ──
        # Only applies when no explicit [action:] directive was set (action=navigate)
        # and no [target:] was given, so we never override what the user wrote.
        for scene in scenes:
            if scene.get("action") == "navigate" and not scene.get("target"):
                inferred = self._infer_nav_action(scene["narration"])
                if inferred:
                    scene["action"] = "click"
                    scene["target"] = f"text={inferred}"
                    logger.info(
                        "Scene %d '%s': auto-inferred click target '%s' from narration",
                        scene["index"], scene["title"], inferred,
                    )

        return scenes

    # ── Narration inference ──────────────────────────────────────────

    def _infer_nav_action(self, narration: str) -> str:
        """
        Scan the narration for navigation intent phrases and return the
        destination label to click, or empty string if none found.

        Examples that trigger inference:
          "Let's open Analytics"           → "Analytics"
          "Navigate to the Agent Status"   → "Agent Status"
          "Now let's go to the Dashboard"  → "Dashboard"
          "Click the Payment Routing tab"  → "Payment Routing"
        """
        for pattern in _NAV_RE:
            m = pattern.search(narration)
            if m:
                label = m.group(1).strip().rstrip(".,;:")
                # Reject overly long matches or ones without a capital letter
                if 2 <= len(label) <= 50 and re.search(r'[A-Z]', label):
                    return label
        return ""

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
