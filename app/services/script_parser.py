"""
Script Parser: converts plain text / Markdown / YAML-frontmatter scripts into
a list of scene dicts with ordered multi-action support.

Scene dict:
  {
    "index": int,
    "title": str,
    "narration": str,
    "url": str | None,
    "actions": [              # ordered list of action steps per scene
        {"action": "click", "target": "...", "value": ""},
        {"action": "type",  "target": "Amount", "value": "1000"},
        {"action": "wait_for", "target": "AI Recommendation", "value": ""},
        {"action": "showcase", "target": "Payment Rail", "value": ""},
    ],
    "action": str,            # first action (backwards-compat)
    "target": str,            # first target (backwards-compat)
    "text": str,              # first value (backwards-compat)
    "wait_for": str,          # legacy pre-action wait selector
  }

Supported actions:
  click, type, scroll, hover, wait_for, focus, showcase, pause, navigate

Script format:
  ## Scene Title
  [url: http://...]
  [action: click]
  [target: Analytics]
  [action: type]
  [target: Amount]
  [value: 1000]
  Narration text for this scene.
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
_NAV_PATTERNS = [
    r"let['\u2019]?s\s+(?:open|go\s+to|navigate\s+to|click\s+on|visit|view|switch\s+to)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$|\s+(?:tab|page|section|module|dashboard|panel|view))",
    r"(?:navigate|go)\s+to\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$|\s+(?:tab|page|section|module|dashboard|panel|view))",
    r"(?:open|click|select|access)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\s+(?:tab|page|section|module|dashboard|panel|view)|\.|,|$)",
    r"now\s+(?:we['\u2019]?ll|let['\u2019]?s|we\s+can)\s+(?:look\s+at|explore|examine|review|see)\s+(?:the\s+)?([A-Z][A-Za-z\s]{2,40}?)(?:\.|,|$)",
]
_NAV_RE = [re.compile(p, re.IGNORECASE) for p in _NAV_PATTERNS]

# Valid action types
_VALID_ACTIONS = {"click", "type", "scroll", "hover", "wait_for", "focus", "showcase", "pause", "navigate"}


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
            scenes = self._parse_paragraphs(script, default_url)

        if scenes and demo_title:
            scenes[0]["demo_title"] = demo_title

        # ── Auto-upgrade navigate → click when narration implies navigation ──
        for scene in scenes:
            actions = scene.get("actions", [])
            has_explicit = any(a["action"] != "navigate" for a in actions)
            if not has_explicit and not any(a.get("target") for a in actions):
                inferred = self._infer_nav_action(scene["narration"])
                if inferred:
                    scene["actions"] = [{"action": "click", "target": f"text={inferred}", "value": ""}]
                    logger.info(
                        "Scene %d '%s': auto-inferred click '%s' from narration",
                        scene["index"], scene["title"], inferred,
                    )

        # ── Backwards-compat: set top-level action/target from first action ──
        for scene in scenes:
            actions = scene.get("actions", [])
            if actions:
                scene["action"] = actions[0]["action"]
                scene["target"] = actions[0].get("target", "")
                scene["text"] = actions[0].get("value", "")
            else:
                scene.setdefault("action", "navigate")
                scene.setdefault("target", "")
                scene.setdefault("text", "")

        return scenes

    # ── Narration inference ──────────────────────────────────────────

    def _infer_nav_action(self, narration: str) -> str:
        for pattern in _NAV_RE:
            m = pattern.search(narration)
            if m:
                label = m.group(1).strip().rstrip(".,;:")
                if 2 <= len(label) <= 50 and re.search(r'[A-Z]', label):
                    return label
        return ""

    # ── Markdown parser ──────────────────────────────────────────────

    def _parse_markdown(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        """Split on ## headings. Supports multiple [action:] blocks per scene."""
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
                if not title:
                    continue
                rest, title = title, f"Scene {len(scenes)+1}"

            # Parse directives and narration
            directives_ordered: List[tuple] = []
            narration_lines = []
            for ln in rest.splitlines():
                m = re.match(r'^\[([a-zA-Z_]+):\s*(.+?)\]$', ln.strip())
                if m:
                    directives_ordered.append((m.group(1).lower(), m.group(2).strip()))
                else:
                    narration_lines.append(ln)

            narration = " ".join(narration_lines).strip()
            if not narration:
                continue

            # Extract scene-level metadata
            url = default_url or None
            wait_for = ""
            duration_override = None
            for key, val in directives_ordered:
                if key == "url":
                    url = val
                elif key == "duration":
                    try:
                        duration_override = float(val)
                    except ValueError:
                        pass

            # Build ordered action list from directives
            actions = self._build_actions(directives_ordered)

            # Legacy: standalone [wait_for:] without being an action
            if not actions:
                for key, val in directives_ordered:
                    if key == "wait_for":
                        wait_for = val
                        break

            scene: Dict[str, Any] = {
                "index": len(scenes),
                "title": title,
                "narration": narration,
                "url": url,
                "actions": actions,
                "wait_for": wait_for,
            }
            if duration_override is not None:
                scene["duration_override"] = duration_override
            scenes.append(scene)

        return scenes

    def _build_actions(self, directives: List[tuple]) -> List[Dict[str, str]]:
        """
        Convert ordered directive tuples into a list of action steps.
        Each [action: X] starts a new step. [target:] and [value:] attach to it.
        """
        actions: List[Dict[str, str]] = []
        current: Optional[Dict[str, str]] = None

        for key, val in directives:
            if key == "action" and val.lower() in _VALID_ACTIONS:
                if current:
                    actions.append(current)
                current = {"action": val.lower(), "target": "", "value": ""}
            elif key == "target":
                if current is None:
                    current = {"action": "click", "target": val, "value": ""}
                else:
                    current["target"] = val
            elif key in ("value", "text"):
                if current is None:
                    current = {"action": "type", "target": "", "value": val}
                else:
                    current["value"] = val
            # url, duration, wait_for are scene-level — skip

        if current:
            actions.append(current)

        return actions

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
                "actions": [],
                "wait_for": "",
            })
        return scenes
