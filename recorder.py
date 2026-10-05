"""Record ONLY the dashboard page to an MP4 — no desktop, other windows, tabs or address bar.

    python recorder.py live 5          # 5-minute video with a slow scroll through the page
    python recorder.py timelapse 24    # one frame every 30 s for 24 hours, played back fast

The page is opened in its own hidden browser, so whatever else you do on the computer never ends up in
the video. The Streamlit toolbar and the system-log panel (it can contain file paths) are hidden.
Stop early with Ctrl+C: the video recorded so far is saved. Files go to ./recordings/.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image
from playwright.async_api import Page, async_playwright

URL = os.getenv("APEX_DASHBOARD_URL", "http://localhost:8501")
OUT_DIR = Path(__file__).parent / "recordings"
WIDTH, HEIGHT = 1920, 1080

HIDE_PRIVATE_JS = """
() => {
  for (const sel of ['header[data-testid="stHeader"]', '[data-testid="stToolbar"]',
                     '[data-testid="stStatusWidget"]', '[data-testid="stDecoration"]']) {
    document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
  }
  // the system log can show local file paths and user names
  document.querySelectorAll('[data-testid="stExpander"]').forEach(e => {
    if ((e.innerText || '').includes('System log')) e.style.display = 'none';
  });
}
"""


async def _open(page: Page) -> None:
    for attempt in range(30):
        try:
            await page.goto(URL, wait_until="networkidle", timeout=15_000)
            await page.wait_for_selector("text=Apex Trade AI", timeout=30_000)
            await page.evaluate(HIDE_PRIVATE_JS)
            return
        except Exception as exc:
            if attempt == 29:
                raise SystemExit(f"Dashboard not reachable at {URL}: {exc}. Start it with start.bat first.")
            print(f"waiting for the dashboard at {URL} ...")
            await asyncio.sleep(5)


async def _frame(page: Page) -> np.ndarray:
    png = await page.screenshot(type="png")
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


async def record(mode: str, amount: float) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"apex_{mode}_{datetime.now():%Y%m%d_%H%M%S}.mp4"
    if mode == "live":
        interval, fps, duration = 1 / 8, 8, amount * 60          # real-time video
    else:
        interval, fps, duration = 30.0, 15, amount * 3600         # 2 hours ≈ 16 s of video
    writer = imageio.get_writer(out, fps=fps, codec="libx264", quality=8, macro_block_size=8,
                                ffmpeg_params=["-pix_fmt", "yuv420p"])
    frames = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path=os.getenv("APEX_CHROMIUM") or None)
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        await _open(page)
        started = last_hide = time.monotonic()
        print(f"Recording {mode} → {out}  (Ctrl+C to stop early)")
        try:
            while time.monotonic() - started < duration:
                tick = time.monotonic()
                if tick - last_hide > 3:                          # Streamlit re-renders: hide again
                    await page.evaluate(HIDE_PRIVATE_JS)
                    last_hide = tick
                if mode == "live":                                # slow scroll down and back up
                    total = await page.evaluate("document.body.scrollHeight") or HEIGHT
                    cycle = 60.0
                    phase = ((tick - started) % cycle) / cycle
                    y = (phase * 2 if phase < 0.5 else (1 - phase) * 2) * max(0, total - HEIGHT)
                    await page.evaluate(f"window.scrollTo(0, {int(y)})")
                frame = await _frame(page)
                # live: repeat the frame if screenshots fell behind, so playback stays real-time
                due = int((time.monotonic() - started) * fps) + 1 if mode == "live" else frames + 1
                for _ in range(max(1, due - frames)):
                    writer.append_data(frame)
                    frames += 1
                if mode == "timelapse" and frames % 20 == 0:
                    print(f"  {frames} frames, {(time.monotonic() - started) / 3600:.1f} h")
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - tick)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("stopped early")
        finally:
            writer.close()
            await browser.close()
    print(f"Saved {frames} frames ({frames / fps:.0f} s of video): {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Record only the Apex dashboard page to MP4")
    ap.add_argument("mode", choices=["live", "timelapse"])
    ap.add_argument("amount", type=float, nargs="?", default=None,
                    help="minutes for live (default 5), hours for timelapse (default 24)")
    a = ap.parse_args()
    amount = a.amount if a.amount is not None else (5 if a.mode == "live" else 24)
    try:
        asyncio.run(record(a.mode, amount))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
