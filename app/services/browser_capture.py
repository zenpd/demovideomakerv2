"""
Browser Capture: uses Playwright (async) to screenshot the target URL.
Returns a list of screenshot file paths for the scene.
"""
import asyncio, logging, os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class BrowserCapture:
    def __init__(self, width: int = 1280, height: int = 720):
        self.width = width
        self.height = height

    async def capture_scene(
        self,
        url: str,
        action: str,
        target: str,
        duration: float,
        output_dir: str,
        scene_index: int,
    ) -> List[str]:
        """
        Navigate to url, optionally perform action, take 1-3 screenshots.
        Returns list of screenshot paths.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed – run: pip install playwright && playwright install chromium")
            return []

        screenshots = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(
                    viewport={"width": self.width, "height": self.height},
                    device_scale_factor=1,
                )
                page = await ctx.new_page()

                # Navigate
                try:
                    await page.goto(url, wait_until="networkidle", timeout=20000)
                except Exception:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception as e:
                        logger.warning("Navigation failed for %s: %s", url, e)

                await asyncio.sleep(1.0)

                # Perform action
                if action == "scroll" and not target:
                    await page.evaluate("window.scrollBy(0, 400)")
                    await asyncio.sleep(0.5)
                elif action == "scroll" and target:
                    try:
                        await page.locator(target).scroll_into_view_if_needed(timeout=3000)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                elif action == "click" and target:
                    try:
                        await page.locator(target).first.click(timeout=3000)
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass

                # Take screenshots (start, optionally mid scene)
                base = os.path.join(output_dir, f"scene_{scene_index:03d}")
                ss1 = f"{base}_a.png"
                await page.screenshot(path=ss1, full_page=False)
                screenshots.append(ss1)

                # 2nd screenshot mid-way for longer scenes
                if duration > 5:
                    await asyncio.sleep(min(duration * 0.3, 3.0))
                    if action == "scroll":
                        await page.evaluate("window.scrollBy(0, 300)")
                        await asyncio.sleep(0.3)
                    ss2 = f"{base}_b.png"
                    await page.screenshot(path=ss2, full_page=False)
                    screenshots.append(ss2)

                await browser.close()

        except Exception as exc:
            logger.exception("BrowserCapture failed scene %d: %s", scene_index, exc)

        return screenshots
