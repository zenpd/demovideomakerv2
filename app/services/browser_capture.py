"""
Browser Capture: uses Playwright (async) to record a live browser video for each scene.
Records actual UI interactions (scrolls, clicks, animations) and returns the video path.
"""
import asyncio, logging, os, shutil, tempfile
from pathlib import Path
from typing import Optional

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
    ) -> Optional[str]:
        """
        Navigate to url, perform action while recording the browser as a video.
        Returns path to the recorded .webm video file, or None on failure.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed – run: pip install playwright && playwright install chromium")
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

                # Navigate
                try:
                    await page.goto(url, wait_until="networkidle", timeout=20000)
                except Exception:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception as e:
                        logger.warning("Navigation failed for %s: %s", url, e)

                await asyncio.sleep(1.5)

                # Perform action(s) spread over the scene duration
                action_budget = max(duration - 2.0, 1.0)

                if action == "scroll" and not target:
                    # Smooth scroll across the scene
                    steps = max(int(action_budget / 0.8), 3)
                    for _ in range(steps):
                        await page.evaluate("window.scrollBy({top: 180, behavior: 'smooth'})")
                        await asyncio.sleep(0.8)
                elif action == "scroll" and target:
                    try:
                        await page.locator(target).scroll_into_view_if_needed(timeout=3000)
                        await asyncio.sleep(1.0)
                        await page.evaluate("window.scrollBy({top: 200, behavior: 'smooth'})")
                        await asyncio.sleep(action_budget - 1.0)
                    except Exception:
                        await asyncio.sleep(action_budget)
                elif action == "click" and target:
                    try:
                        await page.locator(target).first.click(timeout=3000)
                        await asyncio.sleep(action_budget)
                    except Exception:
                        await asyncio.sleep(action_budget)
                elif action == "type" and target:
                    try:
                        await page.locator(target).first.click(timeout=3000)
                        await page.keyboard.type(
                            scene_index and "Demo input text" or "Hello world",
                            delay=80,
                        )
                        await asyncio.sleep(max(action_budget - 1.5, 0.5))
                    except Exception:
                        await asyncio.sleep(action_budget)
                else:
                    # "navigate" or "wait" – just let the page sit
                    await asyncio.sleep(action_budget)

                # Save video reference before closing — path only available after close
                video_obj = page.video

                # Closing the context finalises and saves the recording
                await ctx.close()
                await browser.close()

            # Get path from video object (reliable after context close)
            src_path = None
            if video_obj:
                try:
                    src_path = video_obj.path()
                except Exception as e:
                    logger.warning("video.path() failed: %s", e)

            # Fallback: glob the directory
            if not src_path or not os.path.exists(src_path):
                recorded = list(Path(video_dir).glob("*.webm"))
                src_path = str(recorded[0]) if recorded else None

            if not src_path or not os.path.exists(src_path):
                logger.error("No video recorded for scene %d", scene_index)
                shutil.rmtree(video_dir, ignore_errors=True)
                return None

            file_size = os.path.getsize(src_path)
            if file_size < 1024:
                logger.warning("Scene %d webm is too small (%d bytes) – likely blank", scene_index, file_size)

            dest = os.path.join(output_dir, f"scene_{scene_index:03d}.webm")
            shutil.move(src_path, dest)
            shutil.rmtree(video_dir, ignore_errors=True)
            logger.info("Scene %d recorded: %s (%.1fs, %d bytes)", scene_index, dest, duration, file_size)
            return dest

        except Exception as exc:
            logger.exception("BrowserCapture failed scene %d: %s", scene_index, exc)
            shutil.rmtree(video_dir, ignore_errors=True)
            return None
