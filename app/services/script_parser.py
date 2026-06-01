"""
Smart Demo Narrative Parser v3
================================
Accepts raw narrative text (or structured Markdown/YAML) and converts it into
browser-executable scene dicts with inferred actions.

No ## headings or [directives] required — just paste natural English.

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

Supported actions: navigate, click, type, scroll, hover, showcase
Raw text + Markdown + YAML all supported.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ── Scene-break transition phrases ────────────────────────────────────────────
# Applied at sentence start; force a new scene boundary.
_BREAK_PATTERNS = [
    r"^now[\s,]+(?:let['\u2019]?s|i['\u2019]?(?:ll|d)\s|we['\u2019]?(?:ll|re|ve)\s)",
    r"^(?:moving|switching|jumping|heading)\s+(?:on\s+)?to\b",
    r"^(?:next[,.]?|first[,.]?|second[,.]?|third[,.]?|fourth[,.]?|fifth[,.]?)\s+",
    r"^(?:finally[,.]?|lastly[,.]?|to\s+wrap\s+up[,.]?|in\s+summary[,.]?)\s+",
    r"^let(?:['\u2019]?s|\s+me)\s+(?:now\s+)?(?:take\s+a\s+look\s+at|show\s+you|navigate|explore|walk\s+(?:you\s+)?through|demonstrate|open)\b",
    r"^(?:to\s+(?:start|begin|kick\s+(?:things?\s+)?off)|starting\s+(?:off|with|from))[,.]?\s+",
    r"^over\s+(?:here|on\s+the\s+(?:left|right|top|bottom))[,.]?\s+",
]
_BREAK_RE = [re.compile(p, re.IGNORECASE) for p in _BREAK_PATTERNS]

# ── Action detection patterns ──────────────────────────────────────────────────
# Format: (action_type, pattern, target_group, value_group)
# target_group: which capture group holds the UI element name (or None)
# value_group : which capture group holds text to type (or None)
_ACTION_PATTERNS: List[Tuple[str, str, Optional[int], Optional[int]]] = [

    # NAVIGATE / CLICK — "navigate to the Dashboard", "go to Client Management"
    ("click",
     r"(?:navigate|go|switch|head|jump|move)\s+to\s+(?:the\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,45}?)"
     r"(?=\s*(?:\btab\b|\bpage\b|\bsection\b|\bmodule\b|\bpanel\b|\bview\b|\bscreen\b|"
     r"\bdashboard\b|\bwindow\b|\barea\b|\bfeature\b|\breport\b|\btool\b)"
     r"|\s+(?:to\b|and\b|where\b|which\b|so\b|now\b|in\b|with\b|for\b)|[.,!?]|\s*$)",
     1, None),

    # "open the Pipeline", "click on Analytics", "select the Reports tab"
    ("click",
     r"(?:open|click(?:\s+on)?|select|access|visit|check\s+out|expand|launch|tap)\s+"
     r"(?:the\s+|our\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,45}?)"
     r"(?=\s*(?:\btab\b|\bpage\b|\bsection\b|\bbutton\b|\blink\b|\boption\b|\bitem\b|"
     r"\bmenu\b|\bview\b|\bpanel\b|\bfeature\b|\btool\b)"
     r"|\s+(?:to\b|and\b|where\b|which\b|so\b|now\b|in\b|with\b|for\b)|[.,!?]|\s*$)",
     1, None),

    # "let's open the Dashboard", "let me navigate to Settings"
    ("click",
     r"(?:let['\u2019]?s?|let\s+me|now\s+i['\u2019]?ll|now\s+we['\u2019]?ll)\s+"
     r"(?:\w+\s+){0,3}"
     r"(?:open|go\s+to|navigate\s+to|head\s+to|click(?:\s+on)?|launch|pull\s+up|bring\s+up)\s+"
     r"(?:the\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,45}?)"
     r"(?=\s+(?:to\b|and\b|where\b|which\b|so\b|now\b|in\b)|[.,!?]|\s*$)",
     1, None),

    # "click the Submit button", "press the Confirm button"
    ("click",
     r"(?:i['\u2019]?(?:ll|d\s+like\s+to)|please|you\s+can)\s+"
     r"(?:click|press|hit|tap)\s+(?:the\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,40}?)"
     r"\s+(?:button|link|icon|tab|option|item|control)"
     r"(?=[.,!?]|\s*$)",
     1, None),

    # TYPE / INPUT — "type 'Customer123' in the search box"
    ("type",
     r"(?:type|enter|input|fill\s+(?:in|out)|key\s+in)\s+"
     r"['\"]?([A-Za-z0-9\s@.\-_+$#]{1,80}?)['\"]?\s+"
     r"(?:in(?:to)?|in\s+the|into\s+the)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s]{1,40}?)"
     r"(?=[.,!?]|\s*$)",
     2, 1),

    # "search for 'Acme Corp'", "look up Goldman Sachs"
    ("type",
     r"(?:search\s+(?:for|by)|look\s+up|query\s+for|find)\s+"
     r"['\"]?([A-Za-z0-9\s@.\-_]{1,80}?)['\"]?"
     r"(?:\s+in(?:\s+the)?\s+([A-Za-z][A-Za-z0-9\s]{1,40}))?(?=[.,!?]|\s*$)",
     1, None),

    # HOVER — "hover over the Submit button"
    ("hover",
     r"hover\s+(?:over|on|upon)\s+(?:the\s+|a\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,40}?)"
     r"(?:\s+(?:button|item|card|panel|icon|element|badge|tag|chip|link|row|column))?"
     r"(?=[.,!?]|\s*$)",
     1, None),

    # SCROLL — "scroll down to see", "let's scroll through the list"
    ("scroll",
     r"(?:scroll|swipe)\s+(?:down|up|through|to|along|across)\b",
     None, None),

    # "walk through the list", "browse all the features"
    ("scroll",
     r"\b(?:walk\s+(?:through|down)|browse|run\s+through|go\s+through|"
     r"see\s+all|view\s+all|explore\s+(?:the\s+|all\s+)?(?:list|options|features|items|"
     r"entries|cards|rows|records|cases))\b",
     None, None),

    # SHOWCASE / SPOTLIGHT — "here you can see the Pipeline section"
    ("showcase",
     r"(?:here\s+(?:you\s+)?(?:can\s+)?(?:see|notice|observe|find)|"
     r"look\s+at|notice|observe|point(?:ing)?\s+(?:out|to)|"
     r"draw\s+(?:your\s+)?attention\s+to|highlight(?:ing)?|calling\s+out|"
     r"i['\u2019]?d\s+like\s+to\s+(?:highlight|point\s+out|draw\s+attention\s+to))\s+"
     r"(?:the\s+)?([A-Z][A-Za-z0-9\s&/\-]{1,50}?)"
     r"(?=[.,!?]|\s*$)",
     1, None),
]

# Valid action types
_VALID_ACTIONS = {
    "click", "type", "scroll", "hover", "wait_for",
    "focus", "showcase", "pause", "navigate",
}


def _tc_nouns(text: str) -> List[str]:
    """Extract title-case noun phrases (likely UI element labels)."""
    phrases = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
    if phrases:
        return phrases
    return re.findall(r'\b([A-Z][a-z]{3,})\b', text)


_TRAILING_STOP = re.compile(
    r'\s+(?:to|with|and|of|in|on|for|from|by|that|which|who|are|is|was|'
    r'it|this|those|its|their|our|when|where|as|so|but|the|a|an)\b.*$',
    re.IGNORECASE,
)

# ── Production cue / slide annotation markers ─────────────────────────────────
# Recognises: [PROBLEM — 10s]  [SOLUTION — 30s]  [DEMO]  [SLIDE: Title]
# These appear in demo scripts as timing/section annotations.
# We use them as hard scene boundaries and strip them from TTS narration.
_PROD_CUE_RE = re.compile(
    r'\[([A-Z][A-Za-z0-9\s,\/]+?)(?:\s*[—–\-:]+\s*[^\[\]\n]{0,60})?\](?!\()',
)


def _clean_target(raw: str) -> str:
    """Strip trailing prepositions/stopwords from an inferred UI-element target."""
    cleaned = _TRAILING_STOP.sub('', raw)
    cleaned = cleaned.strip().rstrip('.,;:!? ')
    # Limit to 4 words max (UI labels are never longer)
    words = cleaned.split()
    return ' '.join(words[:4])


class ScriptParser:

    def parse(self, script: str, default_url: str = "") -> List[Dict[str, Any]]:
        script = script.strip()
        if not script:
            return []

        # ── Strip optional YAML frontmatter ──────────────────────────────────
        meta: Dict[str, Any] = {}
        if script.startswith("---") and _HAS_YAML:
            end = script.find("\n---", 3)
            if end != -1:
                try:
                    meta = yaml.safe_load(script[3:end]) or {}
                    script = script[end + 4:].strip()
                except Exception:
                    pass

        demo_title = str(meta.get("title", ""))

        # ── Parse scenes ──────────────────────────────────────────────────────
        if re.search(r'^#{1,3}\s+\S', script, re.MULTILINE):
            scenes = self._parse_markdown(script, default_url)
        else:
            scenes = self._parse_raw(script, default_url)

        if not scenes:
            scenes = [{
                "index": 0, "title": "Demo", "narration": script,
                "url": default_url or None, "actions": [],
                "wait_for": "", "text": "",
            }]

        if scenes and demo_title:
            scenes[0]["demo_title"] = demo_title

        # ── Infer browser actions where none were explicitly set ──────────────
        for scene in scenes:
            acts = scene.get("actions") or []
            has_real = any(
                a.get("action") not in (None, "navigate") or a.get("target")
                for a in acts
            )
            if not has_real:
                inferred = self._infer_action(scene["narration"])
                if inferred:
                    scene["actions"] = [inferred]
                    logger.info(
                        "Scene %d '%s': inferred %s → '%s'",
                        scene["index"], scene["title"],
                        inferred["action"], inferred.get("target", ""),
                    )

        # ── Backwards-compat top-level action/target/text fields ──────────────
        for scene in scenes:
            acts = scene.get("actions") or []
            if acts:
                scene["action"] = acts[0]["action"]
                scene["target"] = acts[0].get("target", "")
                scene["text"]   = acts[0].get("value", "")
            else:
                scene.setdefault("action", "navigate")
                scene.setdefault("target", "")
                scene.setdefault("text", "")

        return scenes

    # ── Raw narrative text parser ──────────────────────────────────────────────

    def _split_by_cues(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        """
        Split script at [LABEL — Ns] production cue markers.
        Each cue starts a new scene; the label becomes the scene title.
        Cue markers are stripped from TTS narration.
        """
        # re.split with one capture group interleaves labels and blocks:
        # [before_first, label1, block1, label2, block2, ...]
        parts = _PROD_CUE_RE.split(text)
        scenes: List[Dict[str, Any]] = []

        # Text before the very first cue (may be empty intro)
        intro = _PROD_CUE_RE.sub('', parts[0]).strip()
        if intro and len(intro.split()) >= 5:
            scenes.append({
                "index": 0, "title": self._auto_title(intro, 0),
                "narration": intro, "url": default_url or None,
                "actions": [], "wait_for": "", "text": "",
            })

        # Interleaved: parts[1::2] = labels, parts[2::2] = narration blocks
        for label, block in zip(parts[1::2], parts[2::2]):
            # Strip any nested cues, leading/trailing quotes, extra whitespace
            narration = _PROD_CUE_RE.sub('', block).strip()
            narration = re.sub(r'^[\u201c\u201d"\']+|[\u201c\u201d"\']+$', '', narration).strip()
            if not narration or len(narration.split()) < 4:
                continue
            title = label.strip().title()
            scenes.append({
                "index": len(scenes),
                "title": title,
                "narration": narration,
                "url": default_url or None,
                "actions": [],
                "wait_for": "",
                "text": "",
            })
        return scenes

    def _parse_raw(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        """Split plain paragraphs into scenes, sub-splitting at transition phrases."""
        # ── Production-cue structured scripts (e.g. [PROBLEM — 10s]) ─────────
        # If the text contains [LABEL — Ns] markers, use them as hard boundaries.
        if _PROD_CUE_RE.search(text):
            scenes = self._split_by_cues(text, default_url)
            if scenes:
                return scenes

        paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
        chunks: List[str] = []
        for para in paras:
            chunks.extend(self._split_at_transitions(para))

        scenes: List[Dict[str, Any]] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk or len(chunk.split()) < 4:
                continue
            scenes.append({
                "index":     len(scenes),
                "title":     self._auto_title(chunk, len(scenes)),
                "narration": chunk,
                "url":       default_url or None,
                "actions":   [],
                "wait_for":  "",
                "text":      "",
            })
        return scenes

    def _split_at_transitions(self, text: str) -> List[str]:
        """Sub-split a paragraph at strong sentence-initial transition phrases."""
        sents = re.split(r'(?<=[.!?])\s+(?=[A-Z"\'])', text)
        if len(sents) <= 2:
            return [text]

        chunks: List[str] = []
        curr: List[str] = [sents[0]]
        for sent in sents[1:]:
            if curr and any(rx.match(sent.strip()) for rx in _BREAK_RE):
                chunks.append(' '.join(curr))
                curr = [sent]
            else:
                curr.append(sent)
        if curr:
            chunks.append(' '.join(curr))
        return chunks or [text]

    def _auto_title(self, text: str, idx: int) -> str:
        """Derive a short scene title from the first sentence."""
        first = re.split(r'(?<=[.!?])\s+', text)[0]
        words = first.split()[:8]
        t = ' '.join(words).rstrip('.!?,;:')
        return (t[:47] + '…') if len(t) > 50 else (t or f"Scene {idx + 1}")

    # ── Action inference ───────────────────────────────────────────────────────

    def _infer_action(self, narration: str) -> Optional[Dict[str, str]]:
        """Match narration against action patterns; return best match or scroll."""
        for action_type, pattern, tgt_grp, val_grp in _ACTION_PATTERNS:
            m = re.search(pattern, narration, re.IGNORECASE)
            if not m:
                continue

            target = ""
            value  = ""

            if tgt_grp and m.lastindex and tgt_grp <= m.lastindex:
                raw = m.group(tgt_grp) or ""
                target = _clean_target(raw)

            if val_grp and m.lastindex and val_grp <= m.lastindex:
                raw = m.group(val_grp) or ""
                value = _clean_target(raw)

            # Sanity: click target must look like a UI label (has capital letter)
            if action_type == "click" and target:
                if not re.search(r'[A-Z]', target):
                    nouns = _tc_nouns(narration)
                    target = nouns[0] if nouns else ""
                    if not target:
                        continue

            logger.debug("Pattern matched: %s → target='%s' value='%s'",
                         action_type, target, value)
            return {"action": action_type, "target": target, "value": value}

        # No pattern matched → smart scroll using a keyword from narration
        nouns = _tc_nouns(narration)
        keyword = nouns[0] if nouns else ""
        return {"action": "scroll", "target": "", "value": keyword}

    # ── Markdown parser (backwards-compatible with v1/v2 scripts) ─────────────

    def _parse_markdown(self, text: str, default_url: str) -> List[Dict[str, Any]]:
        blocks = re.split(r'^#{1,3}\s+', text, flags=re.MULTILINE)
        scenes: List[Dict[str, Any]] = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            title = lines[0].strip() if lines else ""
            rest  = "\n".join(lines[1:]).strip()
            if not rest:
                if not title:
                    continue
                rest, title = title, f"Scene {len(scenes) + 1}"

            directives: List[tuple] = []
            narration_lines: List[str] = []
            for ln in rest.splitlines():
                m = re.match(r'^\[([a-zA-Z_]+):\s*(.+?)\]$', ln.strip())
                if m:
                    directives.append((m.group(1).lower(), m.group(2).strip()))
                else:
                    narration_lines.append(ln)

            narration = " ".join(narration_lines).strip()
            if not narration:
                continue

            url = default_url or None
            wait_for = ""
            dur_override = None
            for key, val in directives:
                if key == "url":
                    url = val
                elif key == "duration":
                    try:
                        dur_override = float(val)
                    except ValueError:
                        pass

            actions = self._build_actions(directives)
            if not actions:
                for key, val in directives:
                    if key == "wait_for":
                        wait_for = val
                        break

            scene: Dict[str, Any] = {
                "index": len(scenes), "title": title,
                "narration": narration, "url": url,
                "actions": actions, "wait_for": wait_for, "text": "",
            }
            if dur_override is not None:
                scene["duration_override"] = dur_override
            scenes.append(scene)
        return scenes

    def _build_actions(self, directives: List[tuple]) -> List[Dict[str, str]]:
        """Convert ordered [action/target/value] directives into action list."""
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
        if current:
            actions.append(current)
        return actions

