import asyncio
import base64
import json
import io
import math
import sys
from PIL import Image
from log_utils import logger
import re
from random import gauss, uniform
from zendriver import cdp


class DailyGreetingLimitReached(Exception):
    """Raised when the platform's daily greeting quota for the current job is exhausted."""


class CaptchaRequired(RuntimeError):
    """Raised when the platform requires verification; stop the current run."""


class PlatformAccessError(RuntimeError):
    """Login or account restrictions prevent further processing."""


class ResumeCaptureError(RuntimeError):
    """The complete resume could not be captured; do not evaluate a partial image."""


class JobPositionNotFound(RuntimeError):
    """Raised when the configured job is absent from the BOSS job selector.

    Continuing after this condition would scan candidates for whichever job was
    selected previously, so callers must stop the current run instead.
    """


# BOSS uses several verification pages: the slider CAPTCHA (verify-slider) and a
# button-click "安全验证" page at /web/passport/zp/verify.html. Match any of them.
# Note: the verify page carries the original destination in a callbackUrl query
# param, so substring matching must target the path, not the whole URL.
CAPTCHA_URL_MARKERS = ('verify-slider', '/passport/zp/verify')


def _url_is_captcha(url: str | None) -> bool:
    """Return True if the URL path belongs to a known CAPTCHA/verification page.

    Only the path (before the query string) is inspected so that a callbackUrl
    pointing back at a normal page does not trigger a false positive.
    """
    path = (url or '').split('?', 1)[0]
    return any(marker in path for marker in CAPTCHA_URL_MARKERS)


async def _any_frame_has_captcha(tab) -> bool:
    """Return True if the main tab or any child frame is showing the CAPTCHA page."""
    if _url_is_captcha(tab.url):
        return True
    frame_tree = await tab.send(cdp.page.get_frame_tree())
    def _walk(node):
        if _url_is_captcha(node.frame.url):
            return True
        return any(_walk(child) for child in (node.child_frames or []))
    return _walk(frame_tree)


async def raise_if_captcha(tab, context: str):
    """Raise immediately when BOSS has redirected the current page or a frame."""
    if await _any_frame_has_captcha(tab):
        raise CaptchaRequired(f"CAPTCHA detected {context}: {tab.url}")
    path = (tab.url or '').split('?', 1)[0]
    if '/web/user/' in path or '/web/passport/' in path:
        raise PlatformAccessError(f"Login required {context}: {path}")
    restricted = await tab.evaluate("""
        (() => {
            const text = document.body ? document.body.innerText : '';
            return text.includes('安全提示') && text.includes('第三方') &&
                text.includes('限制') && text.includes('登录');
        })()
    """)
    if restricted:
        raise PlatformAccessError(f"Account access restricted {context}")


async def _find_or_captcha(tab, text: str):
    """Wrap tab.find(text), raising CaptchaRequired instead of a bare timeout
    when the platform redirected to the CAPTCHA page instead of showing the
    expected element (tab.find has no captcha awareness of its own).
    """
    try:
        return await tab.find(text)
    except TimeoutError:
        await raise_if_captcha(tab, f"while waiting for '{text}'")
        raise


def jitter(mu: float, sigma: float | None = None) -> float:
    """Return a normally-distributed sleep duration centered on mu (sigma defaults to 25% of mu).

    Always returns a positive value: at least max(0.1, mu * 0.3).
    """
    if sigma is None:
        sigma = mu * 0.25
    floor = max(0.1, mu * 0.3)
    return max(mu + gauss(0, sigma), floor)



xpath_age = '//*[@id="recommend-list"]/div/ul/li[{i}]/div/div[1]/div[2]/div[2]/div'
xpath_resume_card_is_viewed = '//*[@id="recommend-list"]/div/ul/li[{i}]'
xpath_resume_card = '//*[@id="recommend-list"]/div/ul/li[{i}]/div/div[1]'
xpath_say_hi = '//button[contains(@class, "btn-sure-v2") and contains(@class, "btn-greet")]'
xpath_i_know_after_say_hi = '//button[contains(text(),"知道了")]'
css_resume_close = '.close-btn'


async def _in_frame(tab, js_body: str):
    """Execute js_body inside the recommendFrame context.

    The JS snippet has access to:
      doc - the iframe's contentDocument
      win - the iframe's contentWindow
    Returns whatever the snippet returns, or None if the frame is absent.
    """
    return await tab.evaluate(f"""
        (function() {{
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame || !frame.contentDocument) return null;
            var doc = frame.contentDocument;
            var win = frame.contentWindow;
            {js_body}
        }})()
    """)


async def _frame_xpath_click(tab, xpath):
    """Click the first element matching xpath inside the recommendFrame."""
    await _in_frame(tab, f"""
        var res = doc.evaluate({repr(xpath)}, doc, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var node = res.singleNodeValue;
        if (node) node.click();
    """)


async def _mouse_click_css(tab, selector: str, warn: bool = True) -> bool:
    """Click a main-page element by CSS selector using a real CDP mouse event (isTrusted=true).
    Returns True if element was found and clicked."""
    pos = await tab.evaluate(f"""
        (function() {{
            var el = document.querySelector({repr(selector)});
            if (!el) return null;
            var r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return null;
            return JSON.stringify({{x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height}});
        }})()
    """)
    if not pos:
        if warn:
            logger.warning("_mouse_click_css: element not found: %s", selector)
        return False
    coords = json.loads(pos)
    await tab.mouse_click(
        coords['x'] + uniform(-0.2, 0.2) * coords['width'],
        coords['y'] + uniform(-0.2, 0.2) * coords['height'],
    )
    return True


async def _frame_mouse_click_xpath(tab, xpath: str, warn: bool = True) -> bool:
    """Click a recommendFrame element by XPath using a real CDP mouse event (isTrusted=true).
    Scrolls the element into view first so getBoundingClientRect() returns in-viewport coords.
    Returns True if element was found and clicked."""
    pos = await tab.evaluate(f"""
        (function() {{
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var res = doc.evaluate({repr(xpath)}, doc, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            var el = res.singleNodeValue;
            if (!el) return null;
            el.scrollIntoView({{block: 'center', behavior: 'instant'}});
            var r = el.getBoundingClientRect();
            return JSON.stringify({{
                x: frameRect.left + r.left + r.width / 2,
                y: frameRect.top + r.top + r.height / 2, width: r.width, height: r.height
            }});
        }})()
    """)
    if not pos:
        if warn:
            logger.warning("_frame_mouse_click_xpath: element not found: %s", xpath)
        return False
    coords = json.loads(pos)
    await tab.mouse_click(
        coords['x'] + uniform(-0.2, 0.2) * coords['width'],
        coords['y'] + uniform(-0.2, 0.2) * coords['height'],
    )
    return True


async def _frame_mouse_click_css(tab, selector: str) -> bool:
    """Click a recommendFrame element by CSS selector using a real CDP mouse event (isTrusted=true).
    Scrolls the element into view first so getBoundingClientRect() returns in-viewport coords.
    Returns True if element was found and clicked."""
    pos = await tab.evaluate(f"""
        (function() {{
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var el = doc.querySelector({repr(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: 'center', behavior: 'instant'}});
            var r = el.getBoundingClientRect();
            return JSON.stringify({{
                x: frameRect.left + r.left + r.width / 2,
                y: frameRect.top + r.top + r.height / 2, width: r.width, height: r.height
            }});
        }})()
    """)
    if not pos:
        logger.warning("_frame_mouse_click_css: element not found: %s", selector)
        return False
    coords = json.loads(pos)
    await tab.mouse_click(
        coords['x'] + uniform(-0.2, 0.2) * coords['width'],
        coords['y'] + uniform(-0.2, 0.2) * coords['height'],
    )
    return True


async def log_in(tab):
    await tab.get('https://www.zhipin.com/web/user/?intent=1')
    await asyncio.sleep(jitter(3))
    results = await tab.xpath('//*[@id="wrap"]/div/div[2]/div[2]/div[1]')
    if results:
        await results[0].click()
        target_url = 'https://www.zhipin.com/web/chat/index'
        for _ in range(60):  # 30s timeout
            await asyncio.sleep(0.5)
            if tab.url == target_url:
                break
            if _url_is_captcha(tab.url):
                raise CaptchaRequired(f"CAPTCHA detected at login: {tab.url}")
        else:
            raise PlatformAccessError("Login did not reach the chat page within 30 seconds")
    else:
        if _url_is_captcha(tab.url):
            raise CaptchaRequired(f"CAPTCHA detected at login: {tab.url}")
        if tab.url != 'https://www.zhipin.com/web/chat/index':
            raise PlatformAccessError(f"Login not confirmed: {tab.url}")
        logger.info("Already logged in.")

    logger.info("Logged in.")
    await asyncio.sleep(jitter(3))


async def ensure_list_view(tab):
    """Switch candidate list to list view if it is currently in grid view."""
    pos = await tab.evaluate("""
        (function() {
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var uses = doc.querySelectorAll('.mode-item use');
            for (var i = 0; i < uses.length; i++) {
                var href = uses[i].getAttribute('xlink:href') || uses[i].getAttribute('href') || '';
                if (href.indexOf('mode1') !== -1) {
                    var btn = uses[i].closest('.mode-item');
                    if (!btn || btn.classList.contains('curr')) return null;
                    var r = btn.getBoundingClientRect();
                    return JSON.stringify({x: frameRect.left + r.left + r.width / 2, y: frameRect.top + r.top + r.height / 2});
                }
            }
            return null;
        })()
    """)
    if pos:
        coords = json.loads(pos)
        await tab.mouse_click(coords['x'], coords['y'])


async def goto_recommend(tab):
    link = await _find_or_captcha(tab, "推荐牛人")
    await link.click()
    # Wait for the recommendFrame iframe to appear
    for _ in range(20):
        # The risk-control redirect happens after clicking the navigation link.
        # Without checking here, the iframe wait expires and the caller continues
        # against the verification page as if recommendation loading had succeeded.
        await raise_if_captcha(tab, "while opening recommendations")
        found = await tab.evaluate(
            '!!document.querySelector(\'iframe[name="recommendFrame"]\')'
        )
        if found:
            break
        await asyncio.sleep(0.5)
    else:
        # Check once more at the timeout boundary in case navigation completed
        # between the last poll and the end of the loop.
        await raise_if_captcha(tab, "while opening recommendations")
    await asyncio.sleep(jitter(2))


async def get_resume_card_text(tab, idx) -> str:
    """Return card text with the explicit gender icon serialized as a field."""
    result = await _in_frame(tab, f"""
        var res = doc.evaluate({repr(xpath_resume_card.format(i=idx))}, doc, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var card = res.singleNodeValue;
        if (!card) return null;
        var icon = card.querySelector('svg.gender use');
        var href = icon && (icon.getAttribute('href') || icon.getAttribute('xlink:href'));
        var gender = href === '#icon-icon-man' ? '男'
            : href === '#icon-icon-woman' ? '女' : null;
        return card.textContent + (gender ? '\\n性别：' + gender : '');
    """)
    return result or ''


async def is_viewed(tab, idx) -> bool:
    result = await _in_frame(tab, f"""
        var res = doc.evaluate({repr(xpath_resume_card_is_viewed.format(i=idx))}, doc, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var card = res.singleNodeValue;
        if (!card) return null;
        var inner = card.querySelector("div[class*='card-inner']");
        return inner ? inner.className : '';
    """)
    if result is None:
        logger.warning(f"#{idx} 加载失败。")
        await scroll_down(tab)
        await asyncio.sleep(jitter(3))
        return False
    return 'has-viewed' in str(result)


async def get_age(tab, idx) -> int:
    result = await _in_frame(tab, f"""
        var res = doc.evaluate({repr(xpath_age.format(i=idx))}, doc, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var elem = res.singleNodeValue;
        return elem ? elem.textContent : null;
    """)
    if not result:
        logger.warning('载入更多简历')
        return 99
    matches = re.findall(r'\d+', result)
    return int(matches[0]) if matches else 99


RESUME_LOAD_TIMEOUT = 30  # includes loading and screenshot stitching

async def _get_c_resume_frame_ids(tab) -> list:
    """Return all c-resume frame IDs in tree order (oldest first)."""
    frame_tree = await tab.send(cdp.page.get_frame_tree())
    result = []
    def walk(node):
        if 'c-resume' in (node.frame.url or ''):
            result.append(node.frame.id_)
        for child in (node.child_frames or []):
            walk(child)
    walk(frame_tree)
    return result


async def _resume_frame_value(tab, context_id, expression):
    """Evaluate screenshot geometry in the resume frame, surfacing script errors."""
    result, error = await tab.send(cdp.runtime.evaluate(
        expression=expression, context_id=context_id, return_by_value=True,
    ))
    if error:
        raise ResumeCaptureError(f"Resume geometry failed: {error.text}")
    return result.value


async def _read_resume_screenshot(tab, frame_id) -> str | None:
    """Capture visible canvas strips and stitch a full PNG without reading canvas pixels.

    Scroll positions are restored even on cancellation. No script elements are inserted; image prototypes, CORS headers and canvas
    contents are left intact.
    """
    context = await tab.send(cdp.page.create_isolated_world(
        frame_id=frame_id, world_name='resume_screenshot',
    ))
    size = await _resume_frame_value(tab, context, """
        (() => {
            const c = document.querySelector('canvas#resume');
            if (!c || !c.width || !c.height || document.readyState !== 'complete') return null;
            if (Array.from(document.images).some(i => !i.complete || !i.naturalWidth)) return null;
            const r = c.getBoundingClientRect();
            if (!r.width || !r.height) return null;
            return {width: r.width, height: r.height};
        })()
    """)
    if size is None:
        return None
    canvas, error = await tab.send(cdp.runtime.evaluate(
        expression="document.querySelector('canvas#resume')", context_id=context,
    ))
    if error:
        raise ResumeCaptureError(error.text)
    await _resume_frame_value(tab, context, """
        (() => {
            const positions = [];
            for (let e = document.querySelector('canvas#resume').parentElement; e; e = e.parentElement)
                positions.push([e, e.scrollLeft, e.scrollTop]);
            globalThis.resumeScrollPositions = positions;
            globalThis.resumeWindowScroll = [scrollX, scrollY];
            return true;
        })()
    """)
    tree = await tab.send(cdp.page.get_frame_tree())
    def frame_path(node, parents):
        path = parents + [node.frame.id_]
        if node.frame.id_ == frame_id:
            return path
        for child in node.child_frames or []:
            found = frame_path(child, path)
            if found:
                return found
        return None
    path = frame_path(tree, [])
    if path is None:
        raise ResumeCaptureError('Resume frame disappeared before capture')
    owners = []
    for fid in path[1:]:
        owner, _ = await tab.send(cdp.dom.get_frame_owner(fid))
        owners.append(owner)
    output = Image.new('RGB', (math.ceil(size['width']), math.ceil(size['height'])))
    offset = 0
    try:
        while offset < output.height:
            await raise_if_captcha(tab, 'while capturing resume')
            visible = await _resume_frame_value(tab, context, f"""
                (() => {{
                    const c = document.querySelector('canvas#resume');
                    for (let e = c.parentElement; e; e = e.parentElement) {{
                        if (e.scrollHeight > e.clientHeight && /auto|scroll|hidden/.test(getComputedStyle(e).overflowY)) {{
                            const r = c.getBoundingClientRect(), p = e.getBoundingClientRect();
                            e.scrollTop += r.top + {offset} - p.top - e.clientTop;
                        }}
                    }}
                    window.scrollBy(0, c.getBoundingClientRect().top + {offset});
                    const r = c.getBoundingClientRect();
                    let top = 0, bottom = innerHeight, left = 0, right = innerWidth;
                    for (let e = c.parentElement; e; e = e.parentElement) {{
                        const p = e.getBoundingClientRect(), style = getComputedStyle(e);
                        if (/auto|scroll|hidden|clip/.test(style.overflowY)) {{
                            top = Math.max(top, p.top + e.clientTop);
                            bottom = Math.min(bottom, p.top + e.clientTop + e.clientHeight);
                        }}
                        if (/auto|scroll|hidden|clip/.test(style.overflowX)) {{
                            left = Math.max(left, p.left + e.clientLeft);
                            right = Math.min(right, p.left + e.clientLeft + e.clientWidth);
                        }}
                    }}
                    return {{top: Math.max(0, top-r.top), bottom: Math.min(r.height, bottom-r.top),
                        left: Math.max(0, left-r.left), right: Math.min(r.width, right-r.left)}};
                }})()
            """)
            # Let the compositor paint after scrolling before taking the screenshot.
            await asyncio.sleep(0.05)
            box = await tab.send(cdp.dom.get_box_model(object_id=canvas.object_id))
            x, y = box.content[0], box.content[1]
            metrics = await tab.send(cdp.page.get_layout_metrics())
            viewport = metrics[3]  # CSS layout viewport (not device pixels)
            top = max(offset, math.ceil(visible['top']), math.ceil(viewport.page_y - y))
            bottom = min(output.height, math.floor(visible['bottom']),
                         math.floor(viewport.page_y + viewport.client_height - y))
            left = max(visible['left'], viewport.page_x - x)
            right = min(visible['right'], viewport.page_x + viewport.client_width - x)
            for owner in owners:
                frame_box = await tab.send(cdp.dom.get_box_model(backend_node_id=owner))
                q = frame_box.content
                top = max(top, math.ceil(q[1] - y))
                bottom = min(bottom, math.floor(q[5] - y))
                left = max(left, q[0] - x)
                right = min(right, q[2] - x)
            if top > offset or bottom <= offset or left > 0.5 or right < size['width'] - 0.5:
                raise ResumeCaptureError('Resume is clipped or cannot be scrolled completely into view')
            encoded = await tab.send(cdp.page.capture_screenshot(
                format_='png', capture_beyond_viewport=False,
                clip=cdp.page.Viewport(x=x, y=y + offset, width=size['width'],
                                       height=bottom-offset, scale=1),
            ))
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as strip:
                output.paste(strip.resize((output.width, bottom-offset)), (0, offset))
            offset = bottom
    finally:
        active_error = sys.exception()
        try:
            await _resume_frame_value(tab, context, """
                (() => {
                    for (const [e, x, y] of globalThis.resumeScrollPositions) e.scrollTo(x, y);
                    window.scrollTo(...globalThis.resumeWindowScroll);
                    delete globalThis.resumeScrollPositions;
                    delete globalThis.resumeWindowScroll;
                    return true;
                })()
            """)
            await tab.send(cdp.runtime.release_object(canvas.object_id))
        except Exception:
            if active_error is None:
                raise
            logger.warning("恢复简历截图状态失败，保留原始异常", exc_info=True)
    if all(low == high for low, high in output.getextrema()):
        return None  # The canvas has dimensions but has not painted its content yet.
    buffer = io.BytesIO()
    output.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


async def get_resume(tab, idx) -> tuple[str | None, str]:
    await asyncio.sleep(jitter(2))

    # Snapshot existing c-resume frames before clicking so we can identify the new one.
    # The recommend page stacks c-resume iframes rather than reusing them, so the
    # currently visible resume is always the newest frame, not the first one.
    existing_fids = set(await _get_c_resume_frame_ids(tab))

    if not await _frame_mouse_click_xpath(tab, xpath_resume_card.format(i=idx)):
        raise ResumeCaptureError(f"Resume card not found: {idx}")

    while True:
        await raise_if_captcha(tab, 'while loading resume')
        all_fids = await _get_c_resume_frame_ids(tab)
        new_fids = [f for f in all_fids if f not in existing_fids]
        if new_fids:
            canvas_base64 = await _read_resume_screenshot(tab, new_fids[-1])
            if canvas_base64 is not None:
                break
        await asyncio.sleep(0.2)

    # Extract the "经历概览" sidebar text from the recommendFrame DOM
    overview_text = await _in_frame(tab, """
        var summary = doc.querySelector('.resume-summary');
        return summary ? summary.innerText.trim() : '';
    """) or ''

    return canvas_base64, overview_text


async def say_hi(tab):
    await asyncio.sleep(jitter(1))
    # Use real mouse click (isTrusted=true) to avoid bot detection.
    # The greet button is inside the recommendFrame iframe, so we combine
    # the iframe's page offset with the button's offset within the iframe.
    pos = await tab.evaluate("""
        (function() {
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var res = doc.evaluate(
                '//button[contains(@class, "btn-sure-v2") and contains(@class, "btn-greet")]',
                doc, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            );
            var btn = res.singleNodeValue;
            if (!btn) return null;
            var btnRect = btn.getBoundingClientRect();
            return JSON.stringify({
                x: frameRect.left + btnRect.left + btnRect.width / 2,
                y: frameRect.top + btnRect.top + btnRect.height / 2
            });
        })()
    """)
    if pos:
        coords = json.loads(pos)
        await tab.mouse_click(coords['x'], coords['y'])
    else:
        # Fallback if iframe/button not found
        await _frame_mouse_click_xpath(tab, xpath_say_hi)
    await asyncio.sleep(jitter(1))

    # Check if the daily limit dialog appeared instead of the normal "知道了" prompt.
    # The platform shows a purchase dialog with ".dialog-wrap.active" containing
    # "今日沟通数已达上限" when the quota is exhausted.
    limit_hit = await tab.evaluate("""
        (function() {
            var d = document.querySelector('.dialog-wrap.active');
            return d ? d.innerText.includes('\u4eca\u65e5\u6c9f\u901a\u6570\u5df2\u8fbe\u4e0a\u9650') : false;
        })()
    """)
    if limit_hit:
        logger.warning("今日沟通数已达上限，关闭对话框并跳过当前职位。")
        await _mouse_click_css(tab, '.boss-popup__close', warn=False)
        await asyncio.sleep(0.5)
        raise DailyGreetingLimitReached("今日沟通数已达上限")

    await _frame_mouse_click_xpath(tab, xpath_i_know_after_say_hi, warn=False)
    # Move mouse away from the top-right nav area to prevent the avatar hover
    # panel from staying open and intercepting subsequent clicks.
    await dismiss_hover_panels(tab)


async def close_resume(tab):
    await asyncio.sleep(jitter(1))
    await _frame_mouse_click_css(tab, css_resume_close)
    # Move mouse away from the top-right area: the close button (.close-btn) lands
    # near the avatar dropdown trigger zone; without this the dropdown can stay
    # open and catch the next click.
    await dismiss_hover_panels(tab)


async def scroll_down(tab):
    await asyncio.sleep(jitter(0.5))
    await _in_frame(tab, "win.scrollTo(0, win.scrollY + 180);")


def _normalize_job_option_label(label):
    """Normalize whitespace without discarding any selector identity fields."""
    return re.sub(r'\s+', ' ', label or '').strip()


def _job_option_business_title(label):
    normalized = _normalize_job_option_label(label)
    return re.split(r'\s+_\s+', normalized, maxsplit=1)[0]


def _select_job_option_label(option_labels, job_title, selector_job_title=None):
    """Resolve exactly one job by business title or an explicit full selector label."""
    title_matches = [
        label for label in option_labels
        if _job_option_business_title(label) == job_title
    ]
    if selector_job_title:
        normalized_selector = _normalize_job_option_label(selector_job_title)
        matches = [
            label for label in title_matches
            if _normalize_job_option_label(label) == normalized_selector
        ]
    else:
        matches = title_matches

    if len(matches) != 1:
        raise JobPositionNotFound(
            f"Expected exactly one job for job_title='{job_title}' and "
            f"selector_job_title='{selector_job_title}', found {len(matches)}; "
            f"candidate full labels={title_matches}. Configure selector_job_title "
            "with the exact full dropdown label to disambiguate."
        )
    return matches[0]


async def select_job_position(tab, job_title, selector_job_title=None):
    await raise_if_captcha(tab, f"before selecting job '{job_title}'")
    await _frame_mouse_click_css(tab, '.ui-dropmenu-label')
    await asyncio.sleep(jitter(1))
    await raise_if_captcha(tab, f"while selecting job '{job_title}'")

    option_labels = await _in_frame(tab, """
        return Array.from(doc.querySelectorAll('ul.job-list li.job-item'))
            .map(function(option) { return option.innerText.trim(); });
    """) or []
    try:
        selected_option_label = _select_job_option_label(
            option_labels, job_title, selector_job_title
        )
    except JobPositionNotFound as error:
        logger.error(str(error))
        raise

    pos = await tab.evaluate(f"""
        (function() {{
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var options = Array.from(doc.querySelectorAll('ul.job-list li.job-item'));
            for (var opt of options) {{
                if (opt.innerText.trim() === {repr(selected_option_label)}) {{
                    opt.scrollIntoView({{block: 'nearest'}});
                    var r = opt.getBoundingClientRect();
                    return JSON.stringify({{x: frameRect.left + r.left + r.width / 2, y: frameRect.top + r.top + r.height / 2}});
                }}
            }}
            return null;
        }})()
    """)
    if not pos:
        message = f"Resolved job option is no longer clickable: {selected_option_label}"
        logger.error(message)
        raise JobPositionNotFound(message)

    coords = json.loads(pos)
    await tab.mouse_click(coords['x'], coords['y'])
    await asyncio.sleep(jitter(5))
    await raise_if_captcha(tab, f"after selecting job '{job_title}'")
    selected_label = await _in_frame(tab, """
        var label = doc.querySelector('.ui-dropmenu-label');
        return label ? label.innerText.trim() : null;
    """)
    if _normalize_job_option_label(selected_label) != _normalize_job_option_label(
        selected_option_label
    ):
        message = (
            f"Job selection mismatch: expected '{selected_option_label}', "
            f"actual '{selected_label}'"
        )
        logger.error(message)
        raise JobPositionNotFound(message)
    logger.info(
        "Selected job confirmed: configured=%s selector_job_title=%s actual=%s",
        job_title,
        selector_job_title,
        selected_label,
    )
    await ensure_list_view(tab)


# XPath constants for 新招呼 list (verified via DevTools)
xpath_greeting_items = '//*[contains(@class,"geek-item-wrap")]'
xpath_greeting_unread_badge = './/span[contains(@class,"badge-count")]'
xpath_greeting_job_sub = './/span[@class="source-job"]'
xpath_chat_job_title = '//*[contains(@class,"source-job")]'  # same class used in list items

GREETING_RESUME_LOAD_TIMEOUT = RESUME_LOAD_TIMEOUT


async def goto_new_greetings(tab) -> int:
    """Navigate to 沟通 -> 新招呼 tab, then switch to 未读 filter.

    Returns the unread count parsed from the tab label (e.g. '新招呼（3）' -> 3),
    or 0 if no badge is shown.
    """
    await tab.get('https://www.zhipin.com/web/chat/index')
    await asyncio.sleep(jitter(2))
    await dismiss_hover_panels(tab)
    link = await _find_or_captcha(tab, "新招呼")
    # The count lives in <em class="num"> inside the same <span class="content">,
    # so link.text (direct text node only) won't include it — query the DOM directly.
    count = await tab.evaluate(r"""
        (function() {
            var spans = document.querySelectorAll('span.content');
            for (var span of spans) {
                for (var node of span.childNodes) {
                    if (node.nodeType === 3 && node.textContent.includes('\u65b0\u62db\u547c')) {
                        var em = span.querySelector('em.num');
                        if (em) {
                            var m = em.innerText.match(/\d+/);
                            return m ? parseInt(m[0]) : 0;
                        }
                    }
                }
            }
            return 0;
        })()
    """) or 0
    await link.click()
    await asyncio.sleep(jitter(2))
    # Click 未读 to show only unread candidates
    unread_tab = await _find_or_captcha(tab, "未读")
    await unread_tab.click()
    await asyncio.sleep(jitter(1))
    return count


async def is_greeting_unread(tab, idx) -> bool | None:
    """True if idx-th list item (1-based) has an unread badge; None if item doesn't exist."""
    items = await tab.xpath(xpath_greeting_items)
    if idx > len(items):
        return None
    item = items[idx - 1]
    badge = await item.query_selector('span[class*="badge-count"]')
    if not badge:
        return False
    return bool(badge.text and badge.text.strip())


async def get_greeting_job_title_at(tab, idx) -> str:
    """Job title text from the idx-th list item."""
    items = await tab.xpath(xpath_greeting_items)
    if idx > len(items):
        return ''
    item = items[idx - 1]
    sub = await item.query_selector('.source-job')
    if sub:
        return (sub.text or '').strip()
    return (item.text or '').strip()


async def open_greeting_at(tab, idx):
    """Click the idx-th candidate to open the chat panel."""
    items = await tab.xpath(xpath_greeting_items)
    if idx <= len(items):
        await items[idx - 1].click()
    await asyncio.sleep(jitter(1.5))


CHAT_PANEL_LOAD_TIMEOUT = 10  # seconds to wait for chat panel .position-name to appear


async def get_current_chat_job_title(tab) -> str:
    """Read the job title from the active chat panel (.position-name inside .conversation-box).

    Polls until .position-name appears, to handle async panel loading after both explicit
    open_greeting_at() clicks and platform auto-navigation after mark_unsuitable().
    Returns '' on timeout.
    """
    for _ in range(CHAT_PANEL_LOAD_TIMEOUT * 2):
        result = await tab.evaluate("""
            (function() {
                var el = document.querySelector('.conversation-box .position-name');
                return el ? el.innerText.trim() : null;
            })()
        """)
        if result:
            return result
        await asyncio.sleep(0.5)
    return ''


async def open_online_resume_greeting(tab):
    """Click 在线简历 in the chat panel header."""
    # The 在线简历 button sits at ~(1438, 139) which falls inside the interview
    # hover-panel's z-index:100 overlay (1093-1553, 50-205).  Move the mouse
    # to a safe area first so the panel collapses before we try to click.
    await dismiss_hover_panels(tab)
    btn = await _find_or_captcha(tab, "在线简历")
    await btn.click()
    await asyncio.sleep(jitter(2))


async def get_online_resume_greeting(tab) -> tuple[str | None, str]:
    """Wait for the visible resume and return a full screenshot and overview."""
    while True:
        await raise_if_captcha(tab, 'while loading greeting resume')
        fids = await _get_c_resume_frame_ids(tab)
        if fids:
            canvas_b64 = await _read_resume_screenshot(tab, fids[-1])
            if canvas_b64 is not None:
                break
        await asyncio.sleep(0.2)

    overview_text = await tab.evaluate("""
        (function() {
            var s = document.querySelector('.resume-summary');
            return s ? s.innerText.trim() : '';
        })()
    """) or ''
    return canvas_b64, overview_text


async def close_online_resume_greeting(tab):
    """Close the online resume modal (.resume-common-dialog) via its main-page .close-btn."""
    await asyncio.sleep(jitter(2))
    await _mouse_click_css(tab, '.resume-common-dialog .close-btn')
    # Wait until modal is fully gone before returning.
    for _ in range(20):
        still_open = await tab.evaluate("""
            (function() {
                var el = document.querySelector('.resume-common-dialog .close-btn');
                return el !== null && el.offsetParent !== null;
            })()
        """)
        if not still_open:
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)  # brief buffer after modal disappears


GREETING_QUALIFIED_MSG = "您好，感谢您的主动联系！您的背景很符合我们的要求，麻烦发一份简历给我看看~"

async def send_chat_message(tab, text: str):
    """Type text into the chat input and click send."""
    # Set content on the contenteditable div and fire input event
    escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    await tab.evaluate(f"""
        (function() {{
            var ed = document.querySelector('.boss-chat-editor-input');
            if (!ed) return;
            ed.focus();
            ed.innerText = `{escaped}`;
            ed.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})()
    """)
    await asyncio.sleep(jitter(0.5))
    send_btn = await _find_or_captcha(tab, "发送")
    await send_btn.click()
    await asyncio.sleep(jitter(1))


async def request_resume(tab):
    """Send greeting message to qualified candidate (求简历 requires mutual reply first)."""
    await send_chat_message(tab, GREETING_QUALIFIED_MSG)


async def mark_unsuitable(tab, reason_category: str):
    """Click 不合适 button, then click the matching preset reason in the dialog."""
    # If the panel is already open (platform kept it open after auto-navigating to this candidate),
    # skip the button click — clicking it again would submit the panel instead of keeping it open.
    panel_open = await tab.evaluate("""
        (function() {
            // Vue preloads .reason-item elements inside a display:none container,
            // so presence in DOM is not enough — check that items are actually visible.
            var items = document.querySelectorAll('.reason-item');
            if (items.length === 0) return false;
            var r = items[0].getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        })()
    """)
    if not panel_open:
        # Get coordinates and use tab.mouse_click — JS .click() and element.click() don't trigger Vue handlers
        pos = await tab.evaluate("""
            (function() {
                var spans = document.querySelectorAll('.operate-btn');
                for (var s of spans) {
                    if (s.innerText.trim() === '不合适') {
                        var r = s.getBoundingClientRect();
                        return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
                    }
                }
                return null;
            })()
        """)
        if not pos:
            raise RuntimeError("Could not find 不合适 button")
        coords = json.loads(pos)
        await tab.mouse_click(coords['x'], coords['y'])
        await asyncio.sleep(jitter(2))

    # Wait until the specific reason item we need is in the DOM (items may load in batches)
    escaped = reason_category.replace("'", "\\'")
    for _ in range(20):
        found = await tab.evaluate(f"""
            (function() {{
                var items = document.querySelectorAll('.reason-item');
                for (var item of items) {{
                    if (item.innerText.trim() === '{escaped}') return true;
                }}
                return false;
            }})()
        """)
        if found:
            break
        await asyncio.sleep(0.3)

    # Click reason item — get coords by text match, then use real mouse click
    reason_pos = await tab.evaluate(f"""
        (function() {{
            var items = document.querySelectorAll('.reason-item');
            for (var item of items) {{
                if (item.innerText.trim() === '{escaped}') {{
                    var r = item.getBoundingClientRect();
                    return JSON.stringify({{x: r.left + r.width / 2, y: r.top + r.height / 2, found: item.outerHTML.substring(0, 100)}});
                }}
            }}
            var available = Array.from(items).map(i => i.innerText.trim()).join(', ');
            return JSON.stringify({{found: null, available: available}});
        }})()
    """)
    reason_data = json.loads(reason_pos) if reason_pos else {}
    if not reason_data.get('found'):
        available = reason_data.get('available', '(empty)')
        raise RuntimeError(f"Could not find reason button: {reason_category!r}, available: {available}")
    await tab.mouse_click(reason_data['x'], reason_data['y'])
    await asyncio.sleep(jitter(1))

    # Confirm if a confirm button appears — get coords then real mouse click
    confirm_pos = await tab.evaluate("""
        (function() {
            var all = document.querySelectorAll('*');
            for (var el of all) {
                if (el.children.length === 0 && el.innerText && el.innerText.trim() === '确定') {
                    var r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
                }
            }
            return null;
        })()
    """)
    if confirm_pos:
        coords = json.loads(confirm_pos)
        await tab.mouse_click(coords['x'], coords['y'])
    await asyncio.sleep(jitter(1))


async def dismiss_hover_panels(tab):
    """Move mouse to a neutral area to dismiss CSS hover-triggered panels.

    The top-right navigation bar contains hover-triggered panels (e.g. the
    interview reminder panel with z-index 100).  After any action that may
    leave the mouse near those nav-items, call this to ensure they collapse
    before attempting clicks in the conversation area.
    """
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mouseMoved', x=300, y=700
    ))
    await asyncio.sleep(0.3)


async def close_popover(tab):
    while True:
        await raise_if_captcha(tab, "while closing popovers")
        clicked = await _mouse_click_css(tab, '.iboss-close', warn=False)
        if not clicked:
            break
        await asyncio.sleep(0.5)
