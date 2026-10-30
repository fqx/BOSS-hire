"""Geometry and sequencing tests for optional, manual badge review."""
from dataclasses import replace
import base64
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw

import job_utils
from native_driver import AXNode, NativeDriverError
from native_symbol_review import card_layout_scale, crop_card_badge, review_candidate
from test_badge_recognition import FONT_PATH, badge


def screenshot(width=788, retina=1, offset=(11, 30), color=(40, 90, 240)):
    window = {"x": offset[0], "y": offset[1], "width": width, "height": 965}
    layout = width / 788
    card = {"x": window["x"] + 20 * layout, "y": window["y"] + 150 * layout,
            "width": 716 * layout, "height": 160 * layout}
    scale = card["width"] / 716
    image = Image.new("RGB", (width * retina, 965 * retina), "white")
    draw = ImageDraw.Draw(image)
    def xy(x, y):
        return ((card["x"] - window["x"] + x * scale) * retina,
                (card["y"] - window["y"] + y * scale) * retina)
    draw.rectangle((*xy(5, 12), *xy(45, 70)), fill=(20, 200, 20))
    draw.rectangle((*xy(80, 20), *xy(180, 50)), fill=(0, 0, 0))
    draw.ellipse((*xy(49.5, 27.5), *xy(62.5, 40.5)), fill=color)
    # A white mark exercises a disconnected inner badge region without invoking
    # either the production glyph templates or any model service.
    draw.line((*xy(54, 34), *xy(58, 34)), fill="white", width=max(1, retina))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), window, card


def card_node(rect):
    return AXNode((0, 1, 0), "AXStaticText", "", "候选人，10年，大专", "", rect, ("AXPress",))


def close_node(card, scale=1):
    size = 36 * scale
    return AXNode(card.path[:-1] + (1,), "AXButton", "", "", "",
                  {"x": card.rect["x"] + card.rect["width"] - size, "y": card.rect["y"],
                   "width": size, "height": size}, ("AXPress",))


class BadgeCropGeometryTests(unittest.TestCase):
    @unittest.skipUnless(FONT_PATH, "A system symbol font is required for independent glyph fixtures")
    def test_stretched_card_uses_fixed_close_control_scale_for_badge_and_glyph(self):
        from badge_recognition import classify_badge
        window = {"x": 21, "y": 445, "width": 1051, "height": 820}
        card = card_node({"x": 139, "y": 565, "width": 814.66, "height": 180})
        close = close_node(card, .77)
        scale = card_layout_scale([card, close], card)
        self.assertAlmostEqual(close.rect["height"], 27.72)
        self.assertAlmostEqual(scale, .77)
        image = Image.new("RGB", (2102, 1640), "white")
        center_x = round((card.rect["x"] - window["x"] + 56 * scale) * 2)
        center_y = round((card.rect["y"] - window["y"] + 34 * scale) * 2)
        badge_size = round(24 * scale)
        image.paste(badge("♂", size=badge_size),
                    (center_x - badge_size // 2, center_y - badge_size // 2))
        output = BytesIO()
        image.save(output, format="PNG")
        crop = crop_card_badge(output.getvalue(), window, card.rect, layout_scale=scale)
        self.assertIsNotNone(crop)
        self.assertEqual(classify_badge(Image.open(BytesIO(crop))).symbol, "♂")
        self.assertIsNone(crop_card_badge(output.getvalue(), window, card.rect,
                                         layout_scale=card.rect["width"] / 716))

    @unittest.skipUnless(FONT_PATH, "A system symbol font is required for independent glyph fixtures")
    def test_colored_avatar_corners_are_whitened_without_changing_visible_glyph(self):
        from badge_recognition import classify_badge
        for symbol in ("♂", "♀"):
            with self.subTest(symbol=symbol):
                png, window, card = screenshot(retina=2)
                image = Image.open(BytesIO(png)).convert("RGB")
                center_x = round((card["x"] - window["x"] + 56) * 2)
                center_y = round((card["y"] - window["y"] + 34) * 2)
                ImageDraw.Draw(image).rectangle((center_x - 16, center_y - 16,
                                                center_x + 16, center_y + 16), fill="white")
                image.paste(badge(symbol, size=24), (center_x - 12, center_y - 12))
                clean = BytesIO()
                image.save(clean, format="PNG")
                clean_crop = crop_card_badge(clean.getvalue(), window, card, layout_scale=1)
                self.assertIsNotNone(clean_crop)
                self.assertEqual(classify_badge(Image.open(BytesIO(clean_crop))).symbol, symbol)

                # An adjacent avatar fragment lies in the square crop but
                # outside the circular badge. It must not reach the matcher.
                avatar_blue = (10, 50, 200)
                ImageDraw.Draw(image).rectangle((center_x - 12, center_y - 12,
                                                center_x - 10, center_y - 10), fill=avatar_blue)
                dirty = BytesIO()
                image.save(dirty, format="PNG")
                dirty_crop = crop_card_badge(dirty.getvalue(), window, card, layout_scale=1)
                self.assertIsNotNone(dirty_crop)
                isolated = Image.open(BytesIO(dirty_crop)).convert("RGB")
                self.assertNotIn(avatar_blue, set(isolated.get_flattened_data()))
                self.assertEqual(isolated.getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(classify_badge(isolated).symbol, symbol)

    def test_crop_tracks_window_offset_layout_width_and_retina_without_portrait_or_text(self):
        for width, retina, offset in ((699, 1, (11, 30)), (788, 1, (123, 445)),
                                      (788, 2, (-500, 70)), (699, 2, (0, 0))):
            with self.subTest(width=width, retina=retina, offset=offset):
                png, window, card = screenshot(width, retina, offset)
                crop = crop_card_badge(png, window, card, layout_scale=card["width"] / 716)
                self.assertIsNotNone(crop)
                image = Image.open(BytesIO(crop)).convert("RGB")
                colors = set(image.get_flattened_data())
                self.assertIn((40, 90, 240), colors)
                self.assertNotIn((20, 200, 20), colors)
                self.assertNotIn((0, 0, 0), colors)
                self.assertLess(image.width, 20 * retina)
                self.assertLess(image.height, 20 * retina)

    def test_crop_can_locate_pink_component_without_interpreting_its_symbol(self):
        pink = (240, 70, 130)
        png, window, card = screenshot(color=pink)
        crop = crop_card_badge(png, window, card, layout_scale=card["width"] / 716)
        self.assertIsNotNone(crop)
        self.assertIn(pink, set(Image.open(BytesIO(crop)).convert("RGB").get_flattened_data()))

    def test_solid_colored_avatar_square_inside_search_region_is_not_a_badge(self):
        for width, retina in ((699, 1), (788, 1), (788, 2)):
            with self.subTest(width=width, retina=retina):
                png, window, card = screenshot(width=width, retina=retina)
                image = Image.open(BytesIO(png)).convert("RGB")
                scale = card["width"] / 716
                box = tuple((card[axis] - window[axis] + offset * scale) * retina
                            for axis, offset in (("x", 49.5), ("y", 27.5), ("x", 62.5), ("y", 40.5)))
                ImageDraw.Draw(image).rectangle(box, fill=(40, 90, 240))
                output = BytesIO()
                image.save(output, format="PNG")
                self.assertIsNone(crop_card_badge(output.getvalue(), window, card,
                                                 layout_scale=card["width"] / 716))

    def test_partially_clipped_badge_search_is_rejected(self):
        png, window, card = screenshot()
        scale = card["width"] / 716
        clip = {"x": window["x"], "y": card["y"] + 34 * scale,
                "width": window["width"], "height": 300}
        self.assertIsNone(crop_card_badge(png, window, card, clip, layout_scale=scale))
        offscreen = {**card, "y": window["y"] - 35 * scale}
        self.assertIsNone(crop_card_badge(png, window, offscreen, layout_scale=scale))


class NativeSymbolReviewTests(unittest.IsolatedAsyncioTestCase):
    def session(self, resume=None):
        png, window, rect = screenshot()
        card = card_node(rect)
        nodes = [card, close_node(card)]
        session = SimpleNamespace(
            wait=AsyncMock(return_value=nodes),
            snapshot=AsyncMock(return_value=nodes),
            candidate_cards=Mock(return_value=[(card, "stable candidate card")]),
            driver=SimpleNamespace(perform=AsyncMock(), capture_window=AsyncMock(return_value=(png, window))),
            read_resume=AsyncMock(return_value=resume or "32岁 大专\n求职期望\n面议\n工作经历\n医药销售"),
            return_to_candidates=AsyncMock(),
        )
        return session, card

    async def test_failed_local_prefilter_skips_capture_classifier_and_endpoint_even_with_fallback(self):
        session, _ = self.session()
        requirements = job_utils.get_job_requirements({"age_upper_bound": 20})
        client = Mock()
        with patch("badge_recognition.classify_badge") as classify, \
             patch("symbol_review.recognize_displayed_symbol") as endpoint:
            result = await review_candidate(session, requirements, client=client, model_fallback=True)
        self.assertEqual(result.stage, "age")
        session.driver.capture_window.assert_not_awaited()
        session.return_to_candidates.assert_awaited_once()
        classify.assert_not_called()
        endpoint.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    async def test_happy_path_uses_only_badge_crop_and_never_calls_model_by_default(self):
        session, card = self.session()
        requirements = job_utils.get_job_requirements({"gender": "女"})
        local = SimpleNamespace(color="blue", symbol="unknown", score=0.4)
        with patch("badge_recognition.classify_badge", return_value=local) as classify, \
             patch("symbol_review.recognize_displayed_symbol") as endpoint:
            result = await review_candidate(session, requirements)
        self.assertEqual((result.stage, result.color, result.symbol), ("review", "blue", "unknown"))
        self.assertFalse(result.used_model)
        image = classify.call_args.args[0]
        self.assertLess(image.width, 20)
        self.assertLess(image.height, 20)
        self.assertIsNotNone(result.crop)
        endpoint.assert_not_called()
        session.driver.perform.assert_awaited_once_with(card, "press")
        self.assertEqual(requirements["gender"], "女")

    async def test_candidate_changed_before_capture_never_captures_or_classifies(self):
        session, card = self.session()
        session.candidate_cards.side_effect = [[(card, "original")], [(card, "replacement")]]
        with patch("badge_recognition.classify_badge") as classify, \
             self.assertRaisesRegex(NativeDriverError, "待复核候选人发生变化"):
            await review_candidate(session, job_utils.get_job_requirements({}))
        session.driver.capture_window.assert_not_awaited()
        classify.assert_not_called()

    async def test_missing_or_ambiguous_close_control_stops_before_capture(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                session, card = self.session()
                nodes = [card]
                if ambiguous:
                    close = close_node(card)
                    nodes += [close, replace(close, path=card.path[:-1] + (2,))]
                session.wait.return_value = nodes
                with patch("badge_recognition.classify_badge") as classify, \
                     self.assertRaisesRegex(NativeDriverError, "控件比例"):
                    await review_candidate(session, job_utils.get_job_requirements({}))
                session.driver.capture_window.assert_not_awaited()
                classify.assert_not_called()

    async def test_close_control_scale_change_during_capture_rejects_image(self):
        session, card = self.session()
        session.snapshot.return_value = [card, close_node(card, .77)]
        with patch("native_symbol_review.crop_card_badge") as crop, \
             patch("badge_recognition.classify_badge") as classify, \
             self.assertRaisesRegex(NativeDriverError, "控件比例发生变化"):
            await review_candidate(session, job_utils.get_job_requirements({}))
        session.driver.capture_window.assert_awaited_once()
        crop.assert_not_called()
        classify.assert_not_called()

    async def test_explicit_model_fallback_only_receives_small_crop_when_local_symbol_unknown(self):
        for symbol in ("unknown", "♂"):
            with self.subTest(local_symbol=symbol):
                session, _ = self.session()
                local = SimpleNamespace(color="blue", symbol=symbol, score=0.7)
                client = Mock()
                with patch("badge_recognition.classify_badge", return_value=local), \
                     patch("symbol_review.recognize_displayed_symbol", return_value=SimpleNamespace(symbol="♂")) as endpoint:
                    result = await review_candidate(session, job_utils.get_job_requirements({}),
                                                    client=client, model_fallback=True)
                if symbol == "unknown":
                    endpoint.assert_called_once()
                    self.assertIs(endpoint.call_args.args[0], client)
                    uploaded = base64.b64decode(endpoint.call_args.args[1])
                    self.assertEqual(uploaded, result.crop)
                    self.assertLess(Image.open(BytesIO(uploaded)).width, 20)
                    self.assertTrue(result.used_model)
                else:
                    endpoint.assert_not_called()
                    self.assertFalse(result.used_model)

    async def test_no_plausible_glyph_never_initializes_or_calls_model_even_when_opted_in(self):
        for color, score in (("blue", 0.0), ("pink", 0.39), ("unknown", 0.9)):
            with self.subTest(color=color, score=score):
                session, _ = self.session()
                local = SimpleNamespace(color=color, symbol="unknown", score=score)
                with patch("badge_recognition.classify_badge", return_value=local), \
                     patch("symbol_review.recognize_displayed_symbol") as endpoint, \
                     patch("openai.OpenAI") as create_client:
                    result = await review_candidate(session, job_utils.get_job_requirements({}), model_fallback=True)
                endpoint.assert_not_called()
                create_client.assert_not_called()
                self.assertFalse(result.used_model)
                self.assertEqual(result.symbol, "unknown")

    async def test_candidate_or_geometry_changed_during_capture_never_uses_image(self):
        for change in ("identity", "position"):
            with self.subTest(change=change):
                session, card = self.session()
                after = (card, "replacement") if change == "identity" else (
                    replace(card, rect={**card.rect, "y": card.rect["y"] + 1}), "original")
                session.candidate_cards.side_effect = [[(card, "original")], [(card, "original")], [after]]
                with patch("native_symbol_review.crop_card_badge") as crop, \
                     patch("badge_recognition.classify_badge") as classify, \
                     self.assertRaisesRegex(NativeDriverError, "截图期间候选人或卡片位置发生变化"):
                    await review_candidate(session, job_utils.get_job_requirements({}))
                session.driver.capture_window.assert_awaited_once()
                crop.assert_not_called()
                classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
