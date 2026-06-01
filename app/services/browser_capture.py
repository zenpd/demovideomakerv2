"""
Browser Capture – Playwright live video recorder with visual interaction feedback.

For each scene:
  1. Navigate to the URL, wait for the SPA to fully render
  2. Inject a visible cursor + click-ripple overlay via JavaScript
  3. Perform the scene action (scroll / click / type / hover / navigate)
     with smooth animations visible in the recording
  4. Close context → Playwright saves the .webm recording
"""
import asyncio
import logging
import os
import random
import re
import shutil
from pathlib import Path
from typing import Optional

# Max times _click will retry after an element-detach or stale-element error
_MAX_CLICK_RETRIES = 3

logger = logging.getLogger(__name__)

# JavaScript injected into every recorded page.
# Provides: cursor dot, click-ripple, element spotlight/glow, smooth cursor movement.
_CURSOR_JS = """
() => {
    if (document.getElementById('__dv_cur__')) return;

    // ── CSS animations ────────────────────────────────────────────────────────
    const style = document.createElement('style');
    style.id = '__dv_styles__';
    style.textContent = `
        @keyframes __dv_ripple {
            0%   { transform: translate(-50%,-50%) scale(0.2); opacity: 0.9; }
            100% { transform: translate(-50%,-50%) scale(3.2); opacity: 0;   }
        }
        @keyframes __dv_pulse {
            0%,100% { box-shadow: 0 0 0 3px rgba(239,68,68,.35), 0 2px 10px rgba(0,0,0,.5); }
            50%     { box-shadow: 0 0 0 8px rgba(239,68,68,.12), 0 2px 10px rgba(0,0,0,.5); }
        }
        @keyframes __dv_glow {
            0%,100% { box-shadow: 0 0 0 9999px rgba(0,0,0,0.35),
                       0 0 0 3px rgba(88,166,255,1.0), 0 0 28px rgba(88,166,255,.8); }
            50%     { box-shadow: 0 0 0 9999px rgba(0,0,0,0.50),
                       0 0 0 4px rgba(88,166,255,1.0), 0 0 52px rgba(88,166,255,1.0); }
        }
        @keyframes __dv_target_ping {
            0%   { transform: scale(1);   opacity: 0.95; }
            70%  { transform: scale(1.6); opacity: 0.4; }
            100% { transform: scale(2.0); opacity: 0; }
        }
        @keyframes __dv_fadein { from { opacity:0; } to { opacity:1; } }

        #__dv_cur__ {
            position: fixed;
            width: 24px; height: 24px;
            background: rgba(239,68,68,.95);
            border: 3px solid rgba(255,255,255,1.0);
            border-radius: 50%;
            pointer-events: none;
            z-index: 2147483647;
            transform: translate(-50%,-50%);
            box-shadow: 0 0 12px rgba(239,68,68,.8), 0 2px 8px rgba(0,0,0,.6);
            animation: __dv_pulse 1.4s ease-in-out infinite;
            display: none;
        }
        .__dv_sp__ {
            position: fixed !important;
            border-radius: 9px !important;
            pointer-events: none !important;
            z-index: 2147483643 !important;
            animation: __dv_glow 1.2s ease-in-out infinite, __dv_fadein .15s ease-out;
            transition: all .2s cubic-bezier(.4,0,.2,1);
        }
        .__dv_sp_ping__ {
            position: fixed !important;
            border-radius: 9px !important;
            pointer-events: none !important;
            z-index: 2147483642 !important;
            border: 3px solid rgba(88,166,255,.85) !important;
            animation: __dv_target_ping 1.0s ease-out infinite;
        }
        .__dv_badge__ {
            position: absolute;
            top: -32px; left: 0;
            background: linear-gradient(135deg, #3882f6, #8b5cf6);
            color: white;
            font: 600 11px/1.4 -apple-system, system-ui, sans-serif;
            padding: 3px 10px 3px 8px;
            border-radius: 5px;
            white-space: nowrap;
            letter-spacing: .02em;
            pointer-events: none;
            box-shadow: 0 2px 8px rgba(56,130,246,.5);
        }
    `;
    document.head.appendChild(style);

    // ── Cursor element ────────────────────────────────────────────────────────
    const cur = document.createElement('div');
    cur.id = '__dv_cur__';
    document.body.appendChild(cur);

    window.__dv_show = (x, y) => {
        cur.style.display = 'block';
        cur.style.left = x + 'px';
        cur.style.top  = y + 'px';
    };

    window.__dv_move = (x, y) => {
        cur.style.left = x + 'px';
        cur.style.top  = y + 'px';
    };

    window.__dv_ripple = (x, y) => {
        const r = document.createElement('div');
        r.style.cssText = [
            'position:fixed', 'pointer-events:none',
            `left:${x}px`, `top:${y}px`,
            'width:52px', 'height:52px',
            'background:rgba(239,68,68,.15)',
            'border:2px solid rgba(239,68,68,.75)',
            'border-radius:50%',
            'z-index:2147483646',
            'animation:__dv_ripple .55s ease-out forwards',
            'transform:translate(-50%,-50%)',
        ].join(';');
        document.body.appendChild(r);
        setTimeout(() => r && r.remove(), 650);
    };

    // ── Spotlight: glowing focus ring around a UI element ────────────────────
    // selector: CSS selector OR plain text label
    window.__dv_spotlight = (selector, label) => {
        document.querySelectorAll('.__dv_sp__').forEach(e => e.remove());
        if (!selector) return null;

        // Normalise Playwright prefixes
        let bare = String(selector);
        if (/^text=/i.test(bare))   bare = bare.slice(5).trim();
        if (/^[/](.*)[/]/i.test(bare)) bare = bare.replace(/^[/]|[/][a-z]*$/g, '').trim();

        let el = null;
        // 1) CSS selector
        try { el = document.querySelector(bare); } catch(_){}

        // 2) Exact text match in interactive elements
        if (!el) {
            const tags = ['a','button','[role="tab"]','[role="menuitem"]','li','[role="option"]'];
            for (const s of tags) {
                for (const c of document.querySelectorAll(s)) {
                    if (c.textContent.trim().toLowerCase() === bare.toLowerCase() && c.offsetParent) {
                        el = c; break;
                    }
                }
                if (el) break;
            }
        }

        // 3) Partial text match in nav / sidebar
        if (!el) {
            const navSels = 'nav a,nav button,aside a,aside button,[class*="sidebar"] a,[class*="menu"] a,[class*="nav"] a';
            for (const c of document.querySelectorAll(navSels)) {
                if (c.textContent.trim().toLowerCase().includes(bare.toLowerCase()) && c.offsetParent) {
                    el = c; break;
                }
            }
        }

        if (!el || !el.offsetParent) return null;

        el.scrollIntoView({behavior:'smooth', block:'nearest'});
        const r = el.getBoundingClientRect();
        const pad = 8;

        const ring = document.createElement('div');
        ring.className = '__dv_sp__';
        Object.assign(ring.style, {
            left:   (r.left - pad) + 'px',
            top:    (r.top  - pad) + 'px',
            width:  (r.width  + pad*2) + 'px',
            height: (r.height + pad*2) + 'px',
            border: '3px solid rgba(88,166,255,1.0)',
        });

        // Secondary "ping" ring that expands outward for extra visibility
        const ping = document.createElement('div');
        ping.className = '__dv_sp_ping__';
        Object.assign(ping.style, {
            left:   (r.left - pad) + 'px',
            top:    (r.top  - pad) + 'px',
            width:  (r.width  + pad*2) + 'px',
            height: (r.height + pad*2) + 'px',
        });
        document.body.appendChild(ping);

        if (label) {
            const badge = document.createElement('div');
            badge.className = '__dv_badge__';
            badge.textContent = String(label).slice(0, 30);
            ring.appendChild(badge);
        }

        document.body.appendChild(ring);
        return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height};
    };

    window.__dv_unspot = () => {
        document.querySelectorAll('.__dv_sp__, .__dv_sp_ping__').forEach(e => e.remove());
    };
}
"""


class BrowserCapture:
    def __init__(self, width: int = 1280, height: int = 720):
        self.width  = width
        self.height = height

    async def capture_scene(
        self,
        url: str,
        action: str,
        target: str,
        duration: float,
        output_dir: str,
        scene_index: int,
        text: str = "",
        wait_for: str = "",
        narration: str = "",   # used for context-aware scroll/nav fallback
    ) -> Optional[str]:
        """
        Record the browser performing the specified action.
        Returns path to the .webm file, or None on failure.
        narration is forwarded to actions so they can infer scroll targets
        and fallback navigation from the spoken text.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return None

        video_dir = os.path.join(output_dir, f"vid_{scene_index:03d}")
        os.makedirs(video_dir, exist_ok=True)

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--single-process",
                    ],
                )
                ctx = await browser.new_context(
                    viewport={"width": self.width, "height": self.height},
                    device_scale_factor=1,
                    record_video_dir=video_dir,
                    record_video_size={"width": self.width, "height": self.height},
                )
                page = await ctx.new_page()

                # ── Navigate with SPA-friendly strategy ───────────────────
                await self._navigate(page, url, scene_index, action=action)

                # Inject cursor overlay
                try:
                    await page.evaluate(_CURSOR_JS)
                except Exception:
                    pass
                await asyncio.sleep(0.3)

                # ── Perform action ────────────────────────────────────────
                budget = max(duration - 1.8, 1.0)
                action_ok = await self._act(
                    page, action, target, text, budget, wait_for, narration
                )

                # Screenshot on click failure for debugging
                if not action_ok and action == "click":
                    try:
                        ss = os.path.join(output_dir, f"scene_{scene_index:03d}_fail.png")
                        await page.screenshot(path=ss, full_page=False)
                        logger.warning("Scene %d click failed – screenshot: %s", scene_index, ss)
                    except Exception:
                        pass

                # Keep video reference before closing
                video_obj = page.video
                await ctx.close()
                await browser.close()

            # ── Retrieve recorded file ────────────────────────────────────
            src_path = None
            if video_obj:
                try:
                    src_path = await video_obj.path()   # Playwright ≥1.50 async
                except TypeError:
                    try:
                        src_path = video_obj.path()     # Older sync fallback
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("video.path() error: %s", e)

            # Glob fallback
            if not src_path or not os.path.exists(src_path):
                found = list(Path(video_dir).glob("*.webm"))
                src_path = str(found[0]) if found else None

            if not src_path or not os.path.exists(src_path):
                logger.error("Scene %d: no .webm file found", scene_index)
                shutil.rmtree(video_dir, ignore_errors=True)
                return None

            dest = os.path.join(output_dir, f"scene_{scene_index:03d}.webm")
            shutil.move(src_path, dest)
            shutil.rmtree(video_dir, ignore_errors=True)
            logger.info("Scene %d recorded: %.1fs  %d bytes",
                        scene_index, duration, os.path.getsize(dest))
            return dest

        except Exception as exc:
            logger.exception("BrowserCapture scene %d failed: %s", scene_index, exc)
            shutil.rmtree(video_dir, ignore_errors=True)
            return None

    # ── Navigation ────────────────────────────────────────────────────────────

    async def _navigate(self, page, url: str, scene_index: int, action: str = "navigate"):
        """
        SPA-friendly navigation strategy.

        Click scenes: use ONLY domcontentloaded (fires as soon as the HTML DOM is
        parsed, typically <1s). No fallback retries, no render wait.
        _resolve_locator handles waiting for the nav element to become visible.
        This prevents the 15s networkidle timeout from showing the homepage for
        20+ seconds before the click, which desynchronises audio and video.

        Non-click scenes: try networkidle first for best quality, fall back to
        load then domcontentloaded, then wait 3.5s for the SPA to fully render.
        """
        if action == "click":
            # Wait for DOM, then give the SPA 2s to render nav sidebar before
            # we attempt to resolve locators. React/Vue needs time to mount.
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=8000)
            except Exception as e:
                logger.debug("Scene %d click-navigate domcontentloaded failed: %s",
                             scene_index, e)
            await asyncio.sleep(2.0)
            return

        # Non-click path: quality-first with fallbacks
        for wait_until, timeout in [
            ("networkidle", 15000),
            ("load",        12000),
            ("domcontentloaded", 8000),
        ]:
            try:
                await page.goto(url, wait_until=wait_until, timeout=timeout)
                break
            except Exception as e:
                logger.debug("Scene %d navigation (%s) failed: %s",
                             scene_index, wait_until, e)
        else:
            logger.warning("Scene %d: all navigation strategies failed for %s",
                           scene_index, url)

        # Give React/Vue/Angular time to fully render after route mount
        await asyncio.sleep(3.5)

        # Scroll to top so the scene always starts from the beginning of the page
        try:
            await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        except Exception:
            pass
        await asyncio.sleep(0.3)

    # ── Action dispatcher ─────────────────────────────────────────────────────

    async def _act(
        self, page, action: str, target: str, text: str,
        budget: float, wait_for: str = "", narration: str = ""
    ) -> bool:
        """Dispatch to the correct action handler. Returns True on success."""
        if action == "scroll":
            await self._scroll(page, target, budget, narration=narration)
            return True
        elif action == "click":
            return await self._click(page, target, budget, wait_for, narration=narration)
        elif action == "type":
            await self._type(page, target, text, budget)
            return True
        elif action == "hover":
            await self._hover(page, target, budget)
            return True
        elif action == "showcase":
            await self._showcase(page, target, budget, narration=narration)
            return True
        else:
            # navigate / wait – try to infer a nav target from narration, otherwise
            # spotlight notable content then scroll through the page
            if narration:
                # Try narration-driven click on a nav element first
                inferred = await self._smart_nav_from_narration(page, narration)
                if inferred:
                    success = await self._click(page, inferred, budget, wait_for, narration=narration)
                    if success:
                        return True
                # No nav click – spotlight a notable element, then scroll
                spotted = await self._spotlight_notable_element(page, budget)
                if not spotted:
                    keyword = self._extract_keyword(narration)
                    if keyword:
                        await self.smart_scroll_to_keyword(page, keyword, budget)
                        return True
            await self._idle(page, budget)
            return True

    # ── Resilient locator resolver ────────────────────────────────────────────

    async def _resolve_locator(self, page, target: str, timeout: int = 8000):
        """
        Try multiple selector strategies for a target string and return the
        first locator whose element is visible on the page.

        Strategy order:
          1. target as-is  (CSS, data-testid, role=…, XPath, or existing text=…)
          2. Playwright text selector  text=<target>  (case-sensitive)
          3. Case-insensitive text regex  text=/<target>/i
          4. :has-text("<target>")  (partial substring match, case-insensitive)
          5. [aria-label*="<target>" i]  (ARIA label contains, case-insensitive)
        """
        # Strip surrounding quotes if the caller passed e.g. "Analytics"
        bare = target.strip('"\'')

        # Strip a leading "text=" prefix so we can build variants from the label
        if bare.lower().startswith("text="):
            bare = bare[5:].strip()

        # Build candidates: nav-scoped a/button selectors come FIRST so we never
        # accidentally match a page heading (e.g. "Operations Dashboard" h1) instead
        # of the actual sidebar nav link.
        candidates = [
            # 1. Exact nav link/button match inside known nav containers
            f"nav a:has-text(\"{bare}\")",
            f"nav button:has-text(\"{bare}\")",
            f"[role=navigation] a:has-text(\"{bare}\")",
            f"[role=navigation] button:has-text(\"{bare}\")",
            f"aside a:has-text(\"{bare}\")",
            f"[class*=sidebar] a:has-text(\"{bare}\")",
            f"[class*=sidebar] button:has-text(\"{bare}\")",
            f"[class*=menu] a:has-text(\"{bare}\")",
            f"[class*=nav] a:has-text(\"{bare}\")",
            # 2. Role-based selectors
            f"[role=tab]:has-text(\"{bare}\")",
            f"[role=menuitem]:has-text(\"{bare}\")",
            f"[role=treeitem]:has-text(\"{bare}\")",
            # 3. Interactive elements anywhere
            f"a:has-text(\"{bare}\")",
            f"button:has-text(\"{bare}\")",
            # 4. Exact text selector (may match headings – used only as fallback)
            f"text={bare}",
            f"text=/{bare}/i",
            # 5. Original target string as-is (CSS, data-testid, XPath, etc.)
            target,
            f"[aria-label*=\"{bare}\" i]",
        ]

        # Use a short per-candidate timeout so we don't stall 12× on failures.
        # The caller's `timeout` is respected only for the first/most likely candidate.
        per_candidate = min(timeout, 1200)
        for i, sel in enumerate(candidates):
            try:
                loc = page.locator(sel).first
                t = timeout if i == 0 else per_candidate
                await loc.wait_for(state="visible", timeout=t)
                logger.info("Resolved selector '%s' → '%s'", target, sel)
                return loc
            except Exception:
                continue

        logger.warning("_resolve_locator: no visible element found for '%s'", target)
        # ── Final fallback: full DOM sweep + fuzzy text match ─────────────────
        return await self._dom_fuzzy_resolve(page, target)

    async def _dom_fuzzy_resolve(self, page, target: str):
        """
        Full DOM sweep of all interactive visible elements, then fuzzy-match
        the target label against their text / aria-label content.
        Used as the last resort when all CSS/text selector strategies fail.
        """
        bare = target.strip('"\'')
        if bare.lower().startswith("text="):
            bare = bare[5:].strip()
        bare_lower = bare.lower()
        bare_words = [w for w in re.split(r'\W+', bare_lower) if len(w) > 2]
        if not bare_words:
            return None

        try:
            items = await page.evaluate("""() => {
                const SELS = [
                    'nav a','nav button','nav li','nav [role]',
                    'aside a','aside button','aside li',
                    'header a','header button','header [role]',
                    '[role=tab]','[role=menuitem]','[role=option]',
                    '[role=button]','[role=link]','[role=treeitem]',
                    '[class*="sidebar"] a','[class*="sidebar"] button','[class*="sidebar"] li',
                    '[class*="menu"] a','[class*="menu"] button','[class*="menu"] li',
                    '[class*="nav"] a','[class*="nav"] button','[class*="nav"] li',
                    '[class*="tab"] button','[class*="tab"] li',
                    '[class*="item"] a','[class*="link"]',
                    'a[href]','button:not([disabled])',
                ];
                const seen = new Set(); const items = [];
                for (const sel of SELS) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (!el.offsetParent) continue;
                        const text = (el.getAttribute('aria-label') ||
                                      el.getAttribute('title') ||
                                      el.textContent || '').trim().replace(/\\s+/g,' ');
                        if (!text || text.length > 80 || seen.has(text)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        seen.add(text);
                        items.push({text, tag: el.tagName.toLowerCase()});
                    }
                }
                return items;
            }""")
        except Exception as e:
            logger.debug("DOM sweep error: %s", e)
            return None

        if not items:
            return None

        logger.debug("DOM sweep found %d items: %s", len(items),
                     [i['text'] for i in items[:20]])
        for item in items:
            t = item['text'].lower()
            t_words = [w for w in re.split(r'\W+', t) if len(w) > 2]
            # Exact or substring match
            if t == bare_lower:
                score = 100
            elif bare_lower in t or t in bare_lower:
                score = 88
            else:
                overlap = sum(1 for w in bare_words if any(w in tw or tw in w for tw in t_words))
                score = int(75 * overlap / max(len(bare_words), 1)) if overlap else 0
            if score > best_score:
                best_score, best_text = score, item['text']

        if not best_text or best_score < 28:
            logger.warning("DOM fuzzy: no match for '%s' (best=%d, candidates=%d)",
                           target, best_score, len(items))
            return None

        logger.info("DOM fuzzy resolved: '%s' → '%s' (score=%d)", target, best_text, best_score)
        # Try Playwright locators with the matched text, preferring interactive tags
        for sel in [
            f"a:has-text(\"{best_text}\")",
            f"button:has-text(\"{best_text}\")",
            f"[role=tab]:has-text(\"{best_text}\")",
            f"[role=menuitem]:has-text(\"{best_text}\")",
            f"li:has-text(\"{best_text}\")",
            f"text={best_text}",
        ]:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=2000)
                return loc
            except Exception:
                continue
        return None

    # ── Individual actions ────────────────────────────────────────────────────

    async def _idle(self, page, budget: float):
        """Show cursor in viewport centre and hold."""
        cx, cy = self.width // 2, self.height // 3
        try:
            await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
        except Exception:
            pass
        await asyncio.sleep(budget)

    async def smart_scroll_to_keyword(self, page, keyword: str, budget: float):
        """
        DOM text search: find the element containing keyword and scroll to it,
        then continue scrolling slowly for the remainder of the budget.
        Falls back to full-page scroll if keyword not found.
        """
        cx, cy = self.width // 2, self.height // 2
        try:
            await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
        except Exception:
            pass

        found = False
        try:
            info = await page.evaluate(
                """
                (kw) => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (node.textContent.toLowerCase().includes(kw.toLowerCase())) {
                            const el = node.parentElement;
                            if (!el) continue;
                            el.scrollIntoView({behavior: 'smooth', block: 'center'});
                            const r = el.getBoundingClientRect();
                            return {x: r.left + r.width/2, y: r.top + r.height/2};
                        }
                    }
                    return null;
                }
                """,
                keyword,
            )
            if info:
                await page.evaluate(
                    f"window.__dv_show && window.__dv_show({info['x']},{info['y']})"
                )
                await asyncio.sleep(1.2)
                remaining = max(budget - 1.5, 1.0)
                await self._slow_scroll(page, cx, cy, remaining)
                found = True
        except Exception as e:
            logger.debug("smart_scroll_to_keyword '%s' failed: %s", keyword, e)

        if not found:
            await self._slow_scroll(page, cx, cy, budget)

    def _extract_keyword(self, narration: str) -> str:
        """
        Extract the most likely scroll-target keyword from narration.
        Looks for Title Case phrases (proper nouns / section headings).
        """
        # Title-case multi-word phrases first (e.g. "Analytics Dashboard")
        phrases = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', narration)
        if phrases:
            return phrases[0]
        # Single capitalized words (e.g. "Dashboard", "Analytics")
        words = re.findall(r'\b([A-Z][a-z]{4,})\b', narration)
        if words:
            return words[0]
        return ""

    async def _smart_nav_from_narration(self, page, narration: str) -> Optional[str]:
        """
        Lightweight semantic matching: collect visible nav/sidebar item labels from
        the page, then use rapidfuzz (or simple contains) to find the best match
        for the narration's implied navigation target.
        Returns the matched label string or None.
        """
        try:
            nav_texts: list = await page.evaluate(
                """
                () => {
                    const seen = new Set();
                    const result = [];
                    const sels = [
                        'nav a', 'nav button', '[role=navigation] a',
                        '[role=navigation] button', 'aside a', 'aside button',
                        '[class*="sidebar"] a', '[class*="sidebar"] button',
                        '[class*="menu"] a',   '[class*="menu"] button',
                        '[class*="nav"] a',    '[class*="nav"] button',
                    ];
                    for (const sel of sels) {
                        for (const el of document.querySelectorAll(sel)) {
                            const t = el.textContent.trim();
                            if (t && t.length < 60 && !seen.has(t)) {
                                seen.add(t);
                                result.push(t);
                            }
                        }
                    }
                    return result;
                }
                """
            )
        except Exception:
            return None

        if not nav_texts:
            return None

        # Extract candidate keywords from narration (Title Case phrases)
        phrases = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', narration)
        if not phrases:
            return None
        query = ' '.join(phrases[:6])

        # Try rapidfuzz first; fall back to simple substring matching
        try:
            from rapidfuzz import process, fuzz
            # token_set_ratio handles word-level overlap (e.g. "Client Management" → "New Client")
            result = process.extractOne(
                query, nav_texts, scorer=fuzz.token_set_ratio, score_cutoff=40
            )
            if not result:
                result = process.extractOne(
                    query, nav_texts, scorer=fuzz.partial_ratio, score_cutoff=40
                )
            if result:
                logger.info("Narration-inferred nav target: '%s' (score=%d)", result[0], result[1])
                return result[0]
        except ImportError:
            pass

        # Simple word overlap fallback (no rapidfuzz)
        query_words = [w.lower() for w in re.split(r'\W+', query) if len(w) > 3]
        best_nav, best_cnt = None, 0
        for nav_text in nav_texts:
            nav_words = [w.lower() for w in re.split(r'\W+', nav_text) if len(w) > 3]
            cnt = sum(1 for qw in query_words
                      if any(qw in nw or nw in qw for nw in nav_words))
            if cnt > best_cnt:
                best_cnt, best_nav = cnt, nav_text
        if best_nav and best_cnt > 0:
            logger.info("Narration-inferred nav target (word-overlap): '%s' (cnt=%d)",
                        best_nav, best_cnt)
            return best_nav

        return None

    async def _scroll(self, page, target: str, budget: float, narration: str = ""):
        """
        Smooth scroll across the scene duration.
        If target is given, scroll that element into view first.
        If narration is given and no target, use keyword-aware scrolling.
        Otherwise scroll down the full page height over the budget.
        """
        cx, cy = self.width // 2, self.height // 2
        try:
            await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
        except Exception:
            pass
        await asyncio.sleep(0.2)

        if target:
            try:
                info = await page.evaluate(
                    "(sel) => { const el = document.querySelector(sel);"
                    " if(!el) return null;"
                    " el.scrollIntoView({behavior:'smooth',block:'center'});"
                    " const r=el.getBoundingClientRect();"
                    " return {x:r.left+r.width/2, y:r.top+r.height/2}; }",
                    target,
                )
                if info:
                    await page.evaluate(
                        f"window.__dv_show && window.__dv_show({info['x']},{info['y']})"
                    )
                # After scrolling to element, continue scrolling slowly to show context
                remaining = budget - 1.5
                if remaining > 0:
                    await asyncio.sleep(1.5)
                    await self._slow_scroll(page, cx, cy, remaining)
                else:
                    await asyncio.sleep(budget)
                return
            except Exception as e:
                logger.debug("Scroll-to-target failed: %s", e)

        # Narration-aware keyword scroll: find and scroll to the relevant section
        if narration:
            keyword = self._extract_keyword(narration)
            if keyword:
                logger.debug("Scroll using narration keyword: '%s'", keyword)
                await self.smart_scroll_to_keyword(page, keyword, budget)
                return

        # General scroll over the full budget
        try:
            metrics = await page.evaluate(
                "() => ({sh: document.body.scrollHeight,"
                " ih: window.innerHeight, sy: window.scrollY})"
            )
            max_scroll = max(metrics["sh"] - metrics["ih"], 0)
        except Exception:
            max_scroll = 800

        if max_scroll < 50:
            await asyncio.sleep(budget)
            return

        await self._slow_scroll(page, cx, cy, budget, distance=max_scroll * 1.0)

    async def _slow_scroll(self, page, cx: float, cy: float,
                           budget: float, distance: float = 0):
        """
        Scroll in a pause-read-scroll pattern that follows the audio narration:
          - Divide the page into thirds
          - Scroll to each third, then pause so the audience can absorb the content
          - This keeps the visible content in sync with what the narrator is saying
        """
        if distance == 0:
            try:
                metrics = await page.evaluate(
                    "() => ({sh: document.body.scrollHeight, ih: window.innerHeight})"
                )
                distance = max(metrics["sh"] - metrics["ih"], 400) * 1.0
            except Exception:
                distance = 600

        # Split budget: 15% intro hold at top, 85% for actual scrolling
        intro_hold = budget * 0.15
        scroll_budget = budget - intro_hold

        # Hold at top briefly so audience sees the section heading first
        await asyncio.sleep(intro_hold)

        # Divide page into 3 sections; pause between each so audio can catch up
        sections = 3
        section_distance = distance / sections
        # Each section: 60% scrolling time, 40% pause time
        section_budget = scroll_budget / sections
        scroll_time_per_section = section_budget * 0.60
        pause_time_per_section  = section_budget * 0.40

        for section in range(sections):
            # Scroll through this section smoothly
            steps = max(int(scroll_time_per_section / 0.35), 4)
            px_per_step = section_distance / steps
            interval = scroll_time_per_section / steps

            for i in range(steps):
                try:
                    await page.evaluate(
                        f"window.scrollBy({{top:{px_per_step:.0f},behavior:'smooth'}})"
                    )
                    # Human-like cursor drift: random small offset each step
                    nx = cx + random.uniform(-18, 18)
                    ny = cy + random.uniform(-8, 8)
                    await page.evaluate(f"window.__dv_move && window.__dv_move({nx:.1f},{ny:.1f})")
                except Exception:
                    pass
                # Human-like timing: add small random jitter to each scroll interval
                jitter = random.uniform(-0.05, 0.08)
                await asyncio.sleep(max(interval + jitter, 0.05))

            # Pause at this section so audience reads/hears the content
            # (skip final pause to not over-extend last section)
            if section < sections - 1:
                # Add slight random variation to pause duration
                await asyncio.sleep(pause_time_per_section * random.uniform(0.85, 1.10))

    async def _click(
        self, page, target: str, budget: float,
        wait_for: str = "", narration: str = ""
    ) -> bool:
        """
        Move cursor to element → ripple → click → wait for result.
        Retries up to _MAX_CLICK_RETRIES times on stale/detached element errors.
        If explicit target fails entirely, attempts narration-based nav inference.
        Returns True if click succeeded.
        """
        if not target and not narration:
            await self._idle_then_scroll(page, budget)
            return False

        # If no explicit target but narration provided, infer from page nav items
        effective_target = target
        if not effective_target and narration:
            inferred = await self._smart_nav_from_narration(page, narration)
            if inferred:
                logger.info("Using narration-inferred target: '%s'", inferred)
                effective_target = inferred

        if not effective_target:
            await self._idle_then_scroll(page, budget)
            return False

        clicked = False
        tried_narration_infer = False  # only attempt narration fallback once
        for attempt in range(1, _MAX_CLICK_RETRIES + 1):
            try:
                loc = await self._resolve_locator(page, effective_target, timeout=15000)
                if loc is None:
                    logger.warning("Click attempt %d: no element for '%s'", attempt, effective_target)
                    # Try narration-driven nav inference immediately (not just on last attempt)
                    if narration and not tried_narration_infer:
                        tried_narration_infer = True
                        inferred = await self._smart_nav_from_narration(page, narration)
                        if inferred and inferred != effective_target:
                            logger.info("Narration fallback: '%s' -> '%s'", effective_target, inferred)
                            effective_target = inferred
                            continue  # retry the loop with the new nav-matched target
                    break

                await loc.scroll_into_view_if_needed(timeout=4000)
                await asyncio.sleep(random.uniform(0.3, 0.5))  # human-like pre-click pause

                bbox = await loc.bounding_box()
                if not bbox:
                    logger.warning("Click attempt %d: no bounding box for '%s'", attempt, effective_target)
                    await asyncio.sleep(0.5)
                    continue

                cx = bbox["x"] + bbox["width"]  / 2
                cy = bbox["y"] + bbox["height"] / 2

                # ── Spotlight element with extended hold so viewers read it ──
                try:
                    await page.evaluate(
                        "([s,l]) => window.__dv_spotlight && window.__dv_spotlight(s,l)",
                        [effective_target, effective_target[:28]]
                    )
                    await asyncio.sleep(1.2)  # longer hold so audience clearly sees the target
                except Exception:
                    pass

                # ── Cursor travels visibly from page center → sidebar button ──
                # Start from right-of-center so the journey across to the left
                # sidebar is clearly visible in the recording.
                start_x = self.width * random.uniform(0.48, 0.62)
                start_y = self.height * random.uniform(0.38, 0.55)
                await page.mouse.move(start_x, start_y)
                await page.evaluate(
                    f"window.__dv_show && window.__dv_show({start_x:.1f},{start_y:.1f})"
                )
                await asyncio.sleep(0.18)

                # 14-step smooth ease-in-out (clearly visible journey, ~0.85s)
                steps = 14
                for step in range(1, steps + 1):
                    t = step / steps
                    ease = t * t * (3 - 2 * t)  # smoothstep
                    # Add slight natural wobble that fades out near the target
                    wobble = (1 - t) * 0.6
                    ix = start_x + (cx - start_x) * ease + random.uniform(-3, 3) * wobble
                    iy = start_y + (cy - start_y) * ease + random.uniform(-2, 2) * wobble
                    await page.mouse.move(ix, iy)  # real mouse event → triggers CSS :hover
                    await page.evaluate(
                        f"window.__dv_move && window.__dv_move({ix:.1f},{iy:.1f})"
                    )
                    await asyncio.sleep(0.06)

                # Hover for a beat so the sidebar item highlights before clicking
                try:
                    await loc.hover(timeout=3000)
                    await asyncio.sleep(0.35)  # let :hover state show in recording
                except Exception:
                    pass

                # Ripple → click
                await page.evaluate(f"window.__dv_ripple && window.__dv_ripple({cx:.1f},{cy:.1f})")
                await asyncio.sleep(random.uniform(0.10, 0.18))
                await loc.click(timeout=5000)
                clicked = True
                logger.info("Clicked '%s' on attempt %d", effective_target, attempt)
                break

            except Exception as e:
                logger.warning("Click attempt %d failed for '%s': %s", attempt, effective_target, e)
                if attempt < _MAX_CLICK_RETRIES:
                    await asyncio.sleep(0.8 * attempt)  # backoff before retry

        if not clicked:
            logger.warning("All click attempts failed for '%s', falling back", effective_target)
            # Spotlight the most interesting element on the current page rather than idle
            spotted = await self._spotlight_notable_element(page, budget)
            if not spotted:
                await self._idle_then_scroll(page, budget)
            return False

        # Post-click: wait for page to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            try:
                await page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass

        # If wait_for selector provided, pause until target content is visible
        if wait_for:
            try:
                await page.locator(wait_for).first.wait_for(state="visible", timeout=8000)
                logger.debug("wait_for visible: %s", wait_for)
            except Exception as e:
                logger.warning("wait_for failed '%s': %s", wait_for, e)

        # Allow JS/animations to finish rendering (human-like settle pause)
        await asyncio.sleep(random.uniform(3.5, 4.5))

        # Re-inject cursor (SPA re-renders can destroy the overlay)
        try:
            await page.evaluate(_CURSOR_JS)
        except Exception:
            pass

        # Spotlight most notable element on the newly loaded page (cards, charts, headings)
        used = 0.5 + 0.35 + 0.18 + 4.0  # approximate time spent above
        remaining = max(budget - used, 1.5)
        spotted = await self._spotlight_notable_element(page, min(remaining * 0.55, 4.0))
        if not spotted:
            await self._slow_scroll(page, self.width // 2, self.height // 2, remaining)
        return True

    async def _idle_then_scroll(self, page, budget: float):
        """Hold at top for 30% of budget, then scroll through the page."""
        hold = budget * 0.30
        scroll_time = budget - hold
        cx, cy = self.width // 2, self.height // 3
        try:
            await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
        except Exception:
            pass
        await asyncio.sleep(hold)
        await self._slow_scroll(page, cx, cy, scroll_time)

    async def _spotlight_notable_element(self, page, budget: float):
        """
        After navigation, find the most prominent UI element on the new page
        (heading, card, metric, chart) and spotlight it for the viewer.
        Falls back to slow scroll if nothing notable is found.
        """
        try:
            result = await page.evaluate("""() => {
                const SELS = [
                    'h1','h2','h3',
                    '[class*="card"]','[class*="metric"]','[class*="stat"]',
                    '[class*="chart"]','[class*="graph"]','[class*="kpi"]',
                    '[class*="dashboard"]','[class*="summary"]','[class*="panel"]',
                    'table','[role="table"]','[class*="table"]',
                    '[class*="badge"]','[class*="tag"]','[class*="status"]',
                ];
                for (const sel of SELS) {
                    const el = document.querySelector(sel);
                    if (!el || !el.offsetParent) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 60 || r.height < 20) continue;
                    const text = el.textContent.trim().substring(0, 50).replace(/\\s+/g,' ');
                    if (!text) continue;
                    return {text, x: r.left + r.width/2, y: r.top + r.height/2};
                }
                return null;
            }""")
            if result:
                label = result['text'][:28]
                cx, cy = result['x'], result['y']
                try:
                    await page.evaluate(
                        "([s,l]) => window.__dv_spotlight && window.__dv_spotlight(s,l)",
                        [f"text={result['text']}", label]
                    )
                    await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
                except Exception:
                    pass
                hold = min(budget * 0.40, 3.0)
                await asyncio.sleep(hold)
                try:
                    await page.evaluate("window.__dv_unspot && window.__dv_unspot()")
                except Exception:
                    pass
                remaining = budget - hold
                if remaining > 0.5:
                    await self._slow_scroll(page, self.width // 2, self.height // 2, remaining)
                return True
        except Exception as e:
            logger.debug("spotlight_notable_element failed: %s", e)
        return False

    async def _type(self, page, target: str, text: str, budget: float):
        """Click input, type text character-by-character at natural speed."""
        type_text = text.strip() if text.strip() else "Demo text"
        try:
            if target:
                loc = await self._resolve_locator(page, target, timeout=8000)
                if loc:
                    await loc.scroll_into_view_if_needed(timeout=4000)
                    bbox = await loc.bounding_box()
                    if bbox:
                        cx = bbox["x"] + bbox["width"]  / 2
                        cy = bbox["y"] + bbox["height"] / 2
                        await page.evaluate(f"window.__dv_show && window.__dv_show({cx},{cy})")
                        await asyncio.sleep(0.35)
                        await page.evaluate(f"window.__dv_ripple && window.__dv_ripple({cx},{cy})")
                        await asyncio.sleep(0.1)
                    await loc.click(timeout=5000)
                    await asyncio.sleep(0.25)

            # Natural typing: use 60% of budget for key presses
            type_budget = budget * 0.60
            delay_ms = max(int(type_budget * 1000 / max(len(type_text), 1)), 50)
            delay_ms = min(delay_ms, 160)
            await page.keyboard.type(type_text, delay=delay_ms)
            await asyncio.sleep(max(budget - len(type_text) * delay_ms / 1000, 0.4))
        except Exception as e:
            logger.warning("Type action failed '%s': %s", target, e)
            await asyncio.sleep(budget)

    async def _showcase(self, page, target: str, budget: float, narration: str = ""):
        """Spotlight a UI element and hold – great for 'here you can see' moments."""
        effective = target
        if not effective and narration:
            keyword = self._extract_keyword(narration)
            if keyword:
                effective = keyword

        if effective:
            spotlit = False
            try:
                result = await page.evaluate(
                    "([s,l]) => window.__dv_spotlight && window.__dv_spotlight(s,l)",
                    [effective, effective[:28]]
                )
                if result and isinstance(result, dict):
                    cx = result.get('x', self.width // 2)
                    cy = result.get('y', self.height // 2)
                    await page.evaluate(f"window.__dv_show && window.__dv_show({cx:.1f},{cy:.1f})")
                    spotlit = True
            except Exception as e:
                logger.debug("Showcase spotlight failed: %s", e)

            # JS spotlight couldn't find the element — use DOM fuzzy resolver
            if not spotlit:
                loc = await self._dom_fuzzy_resolve(page, effective)
                if loc:
                    try:
                        await loc.scroll_into_view_if_needed(timeout=3000)
                        bbox = await loc.bounding_box()
                        if bbox:
                            cx = bbox["x"] + bbox["width"] / 2
                            cy = bbox["y"] + bbox["height"] / 2
                            await page.evaluate(
                                "([s,l]) => window.__dv_spotlight && window.__dv_spotlight(s,l)",
                                [effective, effective[:28]]
                            )
                            await page.evaluate(
                                f"window.__dv_show && window.__dv_show({cx:.1f},{cy:.1f})"
                            )
                            spotlit = True
                    except Exception as e:
                        logger.debug("DOM fuzzy showcase failed: %s", e)

            if not spotlit:
                # Last resort: spotlight a notable element on the visible page
                await self._spotlight_notable_element(page, budget)
                return

            hold = max(budget - 1.0, budget * 0.88)
            await asyncio.sleep(hold)

            try:
                await page.evaluate("window.__dv_unspot && window.__dv_unspot()")
            except Exception:
                pass
            await asyncio.sleep(max(budget - hold, 0.2))
        else:
            await self._spotlight_notable_element(page, budget)

    async def _hover(self, page, target: str, budget: float):
        """Move cursor visibly to element, spotlight it, and hold (reveals tooltips/dropdowns)."""
        if not target:
            await self._idle(page, budget)
            return
        try:
            loc = await self._resolve_locator(page, target, timeout=8000)
            if loc:
                await loc.scroll_into_view_if_needed(timeout=4000)
                bbox = await loc.bounding_box()
                if bbox:
                    cx = bbox["x"] + bbox["width"]  / 2
                    cy = bbox["y"] + bbox["height"] / 2
                    # Spotlight before hover so viewer sees what we're hovering
                    try:
                        await page.evaluate(
                            "([s,l]) => window.__dv_spotlight && window.__dv_spotlight(s,l)",
                            [target, target[:28]]
                        )
                    except Exception:
                        pass
                    # Travel cursor from center to the element
                    sx = self.width * 0.55
                    sy = self.height * 0.45
                    await page.mouse.move(sx, sy)
                    await page.evaluate(f"window.__dv_show && window.__dv_show({sx:.1f},{sy:.1f})")
                    await asyncio.sleep(0.15)
                    steps = 10
                    for step in range(1, steps + 1):
                        t = step / steps
                        ease = t * t * (3 - 2 * t)
                        ix = sx + (cx - sx) * ease
                        iy = sy + (cy - sy) * ease
                        await page.mouse.move(ix, iy)
                        await page.evaluate(f"window.__dv_move && window.__dv_move({ix:.1f},{iy:.1f})")
                        await asyncio.sleep(0.055)
                    await loc.hover(timeout=5000)
                    await asyncio.sleep(0.5)
            await asyncio.sleep(max(budget - 1.5, 0.5))
        except Exception as e:
            logger.warning("Hover action failed '%s': %s", target, e)
            await asyncio.sleep(budget)
        finally:
            try:
                await page.evaluate("window.__dv_unspot && window.__dv_unspot()")
            except Exception:
                pass
