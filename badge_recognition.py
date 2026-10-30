"""Local matching of explicit badge glyphs for manual visual review."""
from dataclasses import dataclass
from functools import lru_cache
from math import sqrt
from statistics import median
from typing import Literal

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class BadgeRecognition:
    color: Literal["blue", "pink", "unknown"]
    symbol: Literal["♂", "♀", "unknown"]
    score: float = 0.0
    margin: float = 0.0


@lru_cache(maxsize=32)
def _templates(width, height):
    templates = {"♂": [], "♀": []}
    for symbol in templates:
        for scale in (0.85, 1.0, 1.15):
            image = Image.new("L", (128, 128))
            draw = ImageDraw.Draw(image)
            def point(x, y):
                return ((0.5 + (x - 0.5) * scale) * 128,
                        (0.5 + (y - 0.5) * scale) * 128)
            center = (0.41, 0.59) if symbol == "♂" else (0.5, 0.38)
            radius = 0.205
            draw.ellipse((*point(center[0]-radius, center[1]-radius),
                          *point(center[0]+radius, center[1]+radius)), outline=255, width=11)
            segments = [((0.55, 0.45), (0.79, 0.21)), ((0.59, 0.21), (0.79, 0.21)),
                        ((0.79, 0.21), (0.79, 0.41))] if symbol == "♂" else [
                        ((0.5, 0.58), (0.5, 0.86)), ((0.34, 0.73), (0.66, 0.73))]
            for start, end in segments:
                draw.line((point(*start), point(*end)), fill=255, width=11)
            # Match the blur imposed by a small screen badge before normalization.
            image = image.resize((width, height), Image.Resampling.LANCZOS).resize((32, 32), Image.Resampling.BILINEAR)
            for dx in (-2, 0, 2):
                for dy in (-2, 0, 2):
                    shifted = Image.new("L", (32, 32))
                    shifted.paste(image, (dx, dy))
                    templates[symbol].append(tuple(shifted.get_flattened_data()))
    return templates


def classify_badge(image: Image.Image) -> BadgeRecognition:
    """Locate colored ink, then match white glyph shape; color never selects a symbol."""
    if not isinstance(image, Image.Image) or image.width < 5 or image.height < 5:
        return BadgeRecognition("unknown", "unknown")
    image = image.convert("RGB")
    image.thumbnail((128, 128))
    hsv = image.convert("HSV")
    pixels = hsv.load()
    candidates = {}
    for y in range(image.height):
        for x in range(image.width):
            h, s, v = pixels[x, y]
            if s >= 80 and v >= 140:
                color = "blue" if 130 <= h <= 180 else "pink" if h >= 230 or h <= 8 else None
                if color:
                    candidates[x, y] = color
    components = []
    while candidates:
        origin, color = candidates.popitem()
        component, pending = [origin], [origin]
        while pending:
            x, y = pending.pop()
            for point in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if candidates.get(point) == color:
                    del candidates[point]
                    component.append(point)
                    pending.append(point)
        if len(component) >= 8:
            components.append((component, color))
    if not components:
        return BadgeRecognition("unknown", "unknown")
    components.sort(key=lambda item: len(item[0]), reverse=True)
    component, color = components[0]
    left, right = min(x for x, _ in component), max(x for x, _ in component)
    top, bottom = min(y for _, y in component), max(y for _, y in component)
    for enclosed, other_color in components[1:]:
        # The white ring disconnects its colored center from the badge background.
        if other_color != color or any(not (left <= x <= right and top <= y <= bottom) for x, y in enclosed):
            return BadgeRecognition("unknown", "unknown")
        component.extend(enclosed)
    width, height = right-left+1, bottom-top+1
    if min(width, height) < 5 or not 0.7 <= width/height <= 1.4:
        return BadgeRecognition(color, "unknown")
    saturation = median(pixels[x, y][1] for x, y in component)
    mask = Image.new("L", (width, height))
    for y in range(height):
        for x in range(width):
            # The inset excludes the white border around the colored badge.
            if ((x+.5-width/2)/(width*.45))**2 + ((y+.5-height/2)/(height*.45))**2 <= 1:
                _, s, v = pixels[left+x, top+y]
                mask.putpixel((x, y), int(max(0, 1-s/saturation)*255) if v >= 150 else 0)
    observed = tuple(mask.resize((32, 32), Image.Resampling.BILINEAR).get_flattened_data())
    if sum(value > 80 for value in observed) < 25:
        return BadgeRecognition(color, "unknown")
    mean = sum(observed)/len(observed)
    centered = tuple(value-mean for value in observed)
    norm = sqrt(sum(value*value for value in centered))
    scores = {}
    for symbol, templates in _templates(width, height).items():
        scores[symbol] = -1.0
        for template in templates:
            mean = sum(template)/len(template)
            target = tuple(value-mean for value in template)
            denominator = norm * sqrt(sum(value*value for value in target))
            score = sum(a*b for a, b in zip(centered, target))/denominator if denominator else 0.0
            scores[symbol] = max(scores[symbol], score)
    ordered = sorted(scores, key=scores.get, reverse=True)
    best, other = (scores[symbol] for symbol in ordered)
    symbol = ordered[0] if best >= 0.62 and best-other >= 0.12 else "unknown"
    return BadgeRecognition(color, symbol, max(0.0, best), best-other)
