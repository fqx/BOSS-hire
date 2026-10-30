"""Independent rendered glyph fixtures for the local manual-review matcher."""
from pathlib import Path
import unittest

from PIL import Image, ImageDraw, ImageFont

from badge_recognition import classify_badge


FONT_PATH = next((str(path) for path in (
    Path("/System/Library/Fonts/Apple Symbols.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
) if path.exists()), None)


def badge(symbol=None, color="#4388ff", size=16, glyph_size=90, shift=(0, 0)):
    image = Image.new("RGB", (128, 128), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 124, 124), fill=color)
    if symbol is not None:
        font = ImageFont.truetype(FONT_PATH, glyph_size)
        left, top, right, bottom = font.getbbox(symbol)
        draw.text(((128-right+left)/2-left+shift[0], (128-bottom+top)/2-top+shift[1]),
                  symbol, font=font, fill="white")
    return image.resize((size, size), Image.Resampling.LANCZOS)


class BadgeRecognitionTests(unittest.TestCase):
    @unittest.skipUnless(FONT_PATH, "A system symbol font is required for independent glyph fixtures")
    def test_same_glyph_is_recognized_on_both_badge_colors(self):
        for symbol in ("♂", "♀"):
            for color, expected_color in (("#4388ff", "blue"), ("#f3729c", "pink")):
                for size in (12, 16, 24, 32):
                    with self.subTest(symbol=symbol, color=color, size=size):
                        result = classify_badge(badge(symbol, color, size))
                        self.assertEqual(result.symbol, symbol)
                        self.assertEqual(result.color, expected_color)
                        self.assertGreater(result.score, 0.6)

    @unittest.skipUnless(FONT_PATH, "A system symbol font is required for independent glyph fixtures")
    def test_shifted_and_scaled_glyphs(self):
        for symbol in ("♂", "♀"):
            for glyph_size, shift in ((78, (0, 0)), (90, (4, -3)), (96, (-3, 3))):
                with self.subTest(symbol=symbol, size=glyph_size, shift=shift):
                    self.assertEqual(classify_badge(badge(symbol, glyph_size=glyph_size, shift=shift)).symbol, symbol)

    def test_color_without_glyph_never_becomes_symbol(self):
        for color, expected in (("#4388ff", "blue"), ("#f3729c", "pink")):
            with self.subTest(color=color):
                result = classify_badge(badge(color=color))
                self.assertEqual(result.color, expected)
                self.assertEqual(result.symbol, "unknown")
        for image in (Image.new("RGB", (24, 24), "#4388ff"),
                      Image.new("RGB", (24, 24), "#f3729c")):
            self.assertEqual(classify_badge(image).symbol, "unknown")

    @unittest.skipUnless(FONT_PATH, "A system symbol font is required for independent glyph fixtures")
    def test_conflicting_badges_return_unknown(self):
        image = Image.new("RGB", (40, 20), "white")
        image.paste(badge("♂"), (1, 2))
        image.paste(badge("♀"), (22, 2))
        self.assertEqual(classify_badge(image).symbol, "unknown")

    def test_unrelated_image_and_malformed_input_return_unknown(self):
        for image in (Image.new("RGB", (14, 15), "white"),
                      Image.effect_noise((24, 24), 60).convert("RGB"),
                      Image.new("RGBA", (16, 16), (0, 0, 0, 0)),
                      Image.new("RGB", (3, 3), "blue"), None):
            with self.subTest(image=image):
                self.assertEqual(classify_badge(image).symbol, "unknown")

    def test_incomplete_white_ring_is_not_a_gender_symbol(self):
        image = badge(size=32).resize((128, 128))
        draw = ImageDraw.Draw(image)
        draw.ellipse((35, 35, 91, 91), outline="white", width=10)
        self.assertEqual(classify_badge(image.resize((16, 16))).symbol, "unknown")


if __name__ == "__main__":
    unittest.main()
