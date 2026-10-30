"""Opt-in check after manually resizing BOSS; never moves or resizes its window."""
import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
import unittest

from PIL import Image

from badge_recognition import classify_badge
from native_driver import NativeDriver
from native_symbol_review import crop_card_badge, card_layout_scale
from native_workflow import NativeSession, is_candidate_list


@unittest.skipUnless(os.getenv("RUN_NATIVE_UI_TESTS") == "1", "Requires unlocked Mac and running BOSS recommendation list")
class NativeViewportTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_current_window_badge(self):
        asyncio.get_running_loop().set_debug(False)
        driver = NativeDriver()
        session = NativeSession(driver)
        nodes = await session.wait(is_candidate_list, "手动调整尺寸后的推荐列表", stable_for=.8)
        self.addCleanup(driver._build_dir.cleanup)
        cards = session.candidate_cards(nodes)
        self.assertTrue(cards, "需要有可读取的候选人卡片")
        card, identity = cards[0]
        png, window = await driver.capture_window()
        after = await session.snapshot()
        matches = [n for n, content in session.candidate_cards(after) if content == identity]
        self.assertEqual(len(matches), 1, "截图期间候选人必须保持一致")
        self.assertEqual(matches[0].rect, card.rect)
        clip = next((n.rect for n in nodes if n.path == card.path[:-2]), None)
        crop = crop_card_badge(png, window, card.rect, clip, layout_scale=card_layout_scale(nodes, card))
        self.assertIsNotNone(crop, "当前可见首张卡片需有完整徽标")
        image = Image.open(BytesIO(crop))
        result = classify_badge(image)
        pixels = Image.open(BytesIO(png)).size
        artifacts = Path.home() / "codex" / "artifacts" / "badge-window-resize-validation"
        artifacts.mkdir(parents=True, exist_ok=True)
        name = f"window-{window['width']}x{window['height']}-{pixels[0]}x{pixels[1]}"
        (artifacts / f"{name}.png").write_bytes(crop)
        record = {"window": window, "screenshot_pixels": pixels, "crop_pixels": image.size,
                  "symbol": result.symbol, "score": round(result.score, 3)}
        (artifacts / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False), flush=True)
        self.assertNotEqual(result.symbol, "unknown", "当前首张卡片徽标需可辨识")


if __name__ == "__main__":
    unittest.main()
