"""Optional, read-only badge review after inexpensive local resume checks."""
import argparse
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import json
import math
import os
from pathlib import Path

import commentjson
from dotenv import load_dotenv
from PIL import Image

import job_utils
from native_driver import NativeDriver, NativeDriverError
from native_workflow import NativeSession, is_candidate_list, prefilter_resume
from wakelock_utils import WakeLock


@dataclass
class SymbolReview:
    stage: str
    color: str = "unknown"
    symbol: str = "unknown"
    score: float = 0.0
    crop: bytes | None = None
    used_model: bool = False


def card_layout_scale(nodes, card):
    """Read the card's fixed-size close control; card width can stretch alone."""
    controls = [n for n in nodes if n.path[:-1] == card.path[:-1]
                and n.role == "AXButton" and not n.description and "AXPress" in n.actions
                and n.rect["height"] > 0
                and abs(n.rect["width"] - n.rect["height"]) < .5
                and n.rect["x"] > card.rect["x"] + card.rect["width"] / 2
                and 0 <= n.rect["y"] - card.rect["y"] <= n.rect["height"]]
    if len(controls) != 1:
        raise NativeDriverError("无法确定当前卡片的控件比例，停止图标裁切")
    return controls[0].rect["height"] / 36


def crop_card_badge(png, window, card, clip=None, *, layout_scale):
    """Locate a colored badge in the avatar corner of the observed UIKit layout.

    Card coordinates are accessibility points; image dimensions determine the
    physical pixel scale. Callers measure fixed controls rather than infer their
    size from a card whose width can stretch independently.
    Unsupported or clipped layouts return no crop instead of guessing.
    """
    image = Image.open(BytesIO(png)).convert("RGB")
    scale = layout_scale
    sx, sy = image.width / window["width"], image.height / window["height"]
    left, top = card["x"] + 40 * scale, card["y"] + 14 * scale
    right, bottom = card["x"] + 74 * scale, card["y"] + 50 * scale
    clip = window if clip is None else clip
    for bounds in (window, clip):
        if (left < bounds["x"] or top < bounds["y"]
                or right > bounds["x"] + bounds["width"]
                or bottom > bounds["y"] + bounds["height"]):
            return None
    box = (math.floor((left - window["x"]) * sx), math.floor((top - window["y"]) * sy),
           math.ceil((right - window["x"]) * sx), math.ceil((bottom - window["y"]) * sy))
    search = image.crop(box)
    # Colors locate a badge; they never determine the drawn symbol.
    pixels = search.load()
    remaining = set()
    for y in range(search.height):
        for x in range(search.width):
            r, g, b = pixels[x, y]
            if ((b > 140 and b > r + 35 and b > g + 35)
                    or (r > 150 and r > g + 40 and b > g + 20 and r > b + 15)):
                remaining.add((x, y))
    components = []
    while remaining:
        origin = remaining.pop()
        component = {origin}
        stack = [origin]
        while stack:
            x, y = stack.pop()
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        xs, ys = zip(*component)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        w, h = x1 - x0, y1 - y0
        if (x0 == 0 or y0 == 0 or x1 == search.width or y1 == search.height
                or not 0.65 < w / h < 1.5
                or not 8 * scale * sx <= w <= 25 * scale * sx
                or not 8 * scale * sy <= h <= 25 * scale * sy):
            continue
        # A square avatar detail is not the round badge. The white glyph can
        # split the colored region, so permit holes while checking the outline.
        outside = sum(((x + .5 - (x0 + x1) / 2) / (w / 2)) ** 2
                      + ((y + .5 - (y0 + y1) / 2) / (h / 2)) ** 2 > 1.25
                      for x, y in component)
        if outside > len(component) * .08 or not .25 <= len(component) / (w * h) <= .9:
            continue
        # The target point is relative to the card, not to the search's origin.
        target_x = (card["x"] + 56 * scale - window["x"]) * sx - box[0]
        target_y = (card["y"] + 34 * scale - window["y"]) * sy - box[1]
        distance = ((x0 + x1) / 2 - target_x) ** 2 + ((y0 + y1) / 2 - target_y) ** 2
        components.append((distance, (x0, y0, x1, y1)))
    if not components:
        return None
    components.sort()
    _, (x0, y0, x1, y1) = components[0]
    crop_box = (max(0, x0 - 1), max(0, y0 - 1), min(search.width, x1 + 1), min(search.height, y1 + 1))
    crop = search.crop(crop_box)
    # Exclude avatar pixels in the square crop's corners. Keep the circular
    # badge and its antialiased edge; the glyph itself is entirely inside it.
    cx, cy = (x0 + x1) / 2 - crop_box[0], (y0 + y1) / 2 - crop_box[1]
    rx, ry = (x1 - x0) / 2 + .5, (y1 - y0) / 2 + .5
    for y in range(crop.height):
        for x in range(crop.width):
            if ((x + .5 - cx) / rx) ** 2 + ((y + .5 - cy) / ry) ** 2 > 1.04:
                crop.putpixel((x, y), (255, 255, 255))
    output = BytesIO()
    crop.save(output, format="PNG")
    return output.getvalue()


async def review_candidate(session, requirements, *, client=None, model_fallback=False, candidate_index=1):
    """Return review data only; do not change screening fields or contact anyone."""
    from badge_recognition import classify_badge

    if candidate_index < 1:
        raise ValueError("candidate_index must be positive")
    nodes = await session.wait(is_candidate_list, "人工复核候选人列表")
    cards = session.candidate_cards(nodes)
    if candidate_index > len(cards):
        raise NativeDriverError("候选人序号超出当前可读取列表")
    card, identity = cards[candidate_index - 1]
    await session.driver.perform(card, "press")
    text = await session.read_resume()
    # This tool produces a manual-review field. It does not apply a sex filter
    # or write a recognized symbol into the application's screening criteria.
    stage = prefilter_resume(text, {**requirements, "gender": None})
    await session.return_to_candidates()
    if stage != "passed":
        return SymbolReview(stage)
    nodes = await session.wait(is_candidate_list, "截图前确认候选人")
    matches = [n for n, content in session.candidate_cards(nodes) if content == identity]
    if len(matches) != 1:
        raise NativeDriverError("待复核候选人发生变化，停止截图")
    card = matches[0]
    if "AXScrollToVisible" in card.actions:
        await session.driver.perform(card, "scrollToVisible")
        nodes = await session.wait(is_candidate_list, "候选人卡片滚动稳定")
        matches = [n for n, content in session.candidate_cards(nodes) if content == identity]
        if len(matches) != 1:
            raise NativeDriverError("卡片滚动后候选人发生变化，停止截图")
        card = matches[0]
    scale = card_layout_scale(nodes, card)
    png, window = await session.driver.capture_window()
    after = await session.snapshot()
    matches = [n for n, content in session.candidate_cards(after) if content == identity]
    if len(matches) != 1 or matches[0].rect != card.rect:
        raise NativeDriverError("截图期间候选人或卡片位置发生变化，停止识别")
    if card_layout_scale(after, matches[0]) != scale:
        raise NativeDriverError("截图期间卡片控件比例发生变化，停止识别")
    containers = [n for n in nodes if n.path == card.path[:-2]]
    crop = crop_card_badge(png, window, card.rect, containers[0].rect if containers else None, layout_scale=scale)
    if crop is None:
        return SymbolReview("badge_not_located")
    local = classify_badge(Image.open(BytesIO(crop)))
    result = SymbolReview("review", local.color, local.symbol, local.score, crop)
    if model_fallback and local.symbol == "unknown" and local.color != "unknown" and local.score >= .4:
        from symbol_review import recognize_displayed_symbol
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL") or None)
        model = await asyncio.to_thread(recognize_displayed_symbol, client, base64.b64encode(crop).decode("ascii"))
        result.symbol = model.symbol
        result.used_model = True
    return result


async def main():
    parser = argparse.ArgumentParser(description="本地预筛后识别界面符号，仅供人工复核")
    parser.add_argument("-c", "--config")
    parser.add_argument("--job-title")
    parser.add_argument("--candidate-index", type=int, default=1)
    parser.add_argument("--model-fallback", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_dotenv()
    requirements = job_utils.get_job_requirements({})
    title, selector = args.job_title, None
    if args.config:
        with open(args.config, encoding="utf-8") as source:
            configs = commentjson.load(source)
        configs = configs if isinstance(configs, list) else [configs]
        matches = [c for c in configs if title is None or c["job_title"] == title]
        if len(matches) != 1:
            raise ValueError("配置包含多个岗位，请用 --job-title 精确指定")
        title = matches[0]["job_title"]
        requirements = job_utils.get_job_requirements(matches[0]["job_requirements"])
        selector = requirements["selector_job_title"]
    with WakeLock():
        session = NativeSession(NativeDriver())
        if title:
            await session.select_job(title, selector)
        result = await review_candidate(session, requirements, model_fallback=args.model_fallback,
                                        candidate_index=args.candidate_index)
    output = None
    if result.crop:
        output = args.output or Path.home() / "codex" / "artifacts" / f"badge-review-{datetime.now():%Y%m%d-%H%M%S}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(result.crop)
    print(json.dumps({"stage": result.stage, "badge_color": result.color, "displayed_symbol": result.symbol,
                      "match_score": round(result.score, 3), "used_model": result.used_model,
                      "crop": str(output) if output else None, "manual_review_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
