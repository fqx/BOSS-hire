import asyncio
import base64
import json
from log_utils import logger
import re
from random import gauss
from zendriver import cdp


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
xpath_say_hi = '//button[starts-with(@class, "btn-v2 btn-sure-v2 btn-greet")]'
xpath_i_know_after_say_hi = '//button[contains(text(),"知道了")]'
xpath_resume_close = '//i[@class="icon-close"]'


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


async def _mouse_click_css(tab, selector: str) -> bool:
    """Click a main-page element by CSS selector using a real CDP mouse event (isTrusted=true).
    Returns True if element was found and clicked."""
    pos = await tab.evaluate(f"""
        (function() {{
            var el = document.querySelector({repr(selector)});
            if (!el) return null;
            var r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return null;
            return JSON.stringify({{x: r.left + r.width / 2, y: r.top + r.height / 2}});
        }})()
    """)
    if not pos:
        return False
    coords = json.loads(pos)
    await tab.mouse_click(coords['x'], coords['y'])
    return True


async def _frame_mouse_click_xpath(tab, xpath: str) -> bool:
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
                y: frameRect.top + r.top + r.height / 2
            }});
        }})()
    """)
    if not pos:
        return False
    coords = json.loads(pos)
    await tab.mouse_click(coords['x'], coords['y'])
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
                y: frameRect.top + r.top + r.height / 2
            }});
        }})()
    """)
    if not pos:
        return False
    coords = json.loads(pos)
    await tab.mouse_click(coords['x'], coords['y'])
    return True


async def log_in(tab):
    await tab.get('https://www.zhipin.com/web/user/?intent=1')
    await asyncio.sleep(jitter(3))
    results = await tab.xpath('//*[@id="wrap"]/div/div[2]/div[2]/div[1]')
    if results:
        await results[0].click()
    else:
        logger.warning("Maybe we have logged in?")

    target_url = 'https://www.zhipin.com/web/chat/index'
    for _ in range(60):  # 30s timeout
        await asyncio.sleep(0.5)
        if tab.url == target_url:
            break
    else:
        logger.warning("Login timeout")

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
    link = await tab.find("推荐牛人")
    await link.click()
    # Wait for the recommendFrame iframe to appear
    for _ in range(20):
        found = await tab.evaluate(
            '!!document.querySelector(\'iframe[name="recommendFrame"]\')'
        )
        if found:
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(jitter(2))


async def get_resume_card_text(tab, idx) -> str:
    """Return textContent of resume card at position idx."""
    result = await _in_frame(tab, f"""
        var res = doc.evaluate({repr(xpath_resume_card.format(i=idx))}, doc, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var card = res.singleNodeValue;
        return card ? card.textContent : null;
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


RESUME_LOAD_TIMEOUT = 10  # seconds to wait for resume canvas to appear

# Injected into the c-resume iframe HTML to force CORS mode on all <img> loads.
# Combined with adding Access-Control-Allow-Origin: * to image responses via Fetch
# interception, this prevents canvas taint so toDataURL() works without
# --disable-web-security (which triggers bot detection).
_CROSSORIGIN_INJECT = """<script>
(function() {
    var d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    if (d && d.set) {
        Object.defineProperty(HTMLImageElement.prototype, 'src', {
            set: function(v) { if (!this.crossOrigin) this.crossOrigin = 'anonymous'; d.set.call(this, v); },
            get: d.get, configurable: true
        });
    }
    var Orig = window.Image;
    window.Image = function(w, h) { var img = new Orig(w, h); img.crossOrigin = 'anonymous'; return img; };
    window.Image.prototype = Orig.prototype;
})();
</script>"""


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


async def _read_canvas_b64(tab, frame_id) -> str | None:
    """Try canvas.toDataURL() inside frame_id. Returns None if canvas absent or tainted."""
    ctx_id = await tab.send(cdp.page.create_isolated_world(
        frame_id=frame_id, world_name='canvas_read'
    ))
    result, exc = await tab.send(cdp.runtime.evaluate(
        expression="""
            (function() {
                var c = document.querySelector('canvas#resume');
                if (!c) return null;
                try { return c.toDataURL('image/png').substring(22); }
                catch(e) { return null; }
            })()
        """,
        context_id=ctx_id,
        return_by_value=True,
    ))
    if exc or not result or not result.value:
        return None
    return result.value


async def _enable_cors_intercept(tab):
    """Enable Fetch interception to un-taint the resume canvas.

    Intercepts:
    - c-resume HTML responses: injects a <script> that sets crossOrigin='anonymous'
      on all <img> elements before their src is assigned.
    - Image responses: adds Access-Control-Allow-Origin: * so the browser accepts
      the CORS response and does not taint the canvas.
    """
    async def _handle_paused(evt: cdp.fetch.RequestPaused):
        req_id = evt.request_id
        status_code = evt.response_status_code

        if status_code is None:
            await tab.send(cdp.fetch.continue_request(request_id=req_id))
            return

        resource_type = evt.resource_type

        if resource_type == cdp.network.ResourceType.DOCUMENT and 'c-resume' in evt.request.url:
            body, b64_enc = await tab.send(cdp.fetch.get_response_body(request_id=req_id))
            if b64_enc:
                body = base64.b64decode(body).decode('utf-8', errors='replace')
            body = body.replace('<head>', '<head>' + _CROSSORIGIN_INJECT, 1)
            await tab.send(cdp.fetch.fulfill_request(
                request_id=req_id,
                response_code=status_code,
                response_headers=evt.response_headers,
                body=base64.b64encode(body.encode('utf-8')).decode(),
            ))

        elif resource_type == cdp.network.ResourceType.IMAGE:
            body, b64_enc = await tab.send(cdp.fetch.get_response_body(request_id=req_id))
            if not b64_enc:
                body = base64.b64encode(body.encode()).decode()
            headers = [
                h for h in (evt.response_headers or [])
                if h.name.lower() != 'access-control-allow-origin'
            ]
            headers.append(cdp.fetch.HeaderEntry(name='Access-Control-Allow-Origin', value='*'))
            await tab.send(cdp.fetch.fulfill_request(
                request_id=req_id,
                response_code=status_code,
                response_headers=headers,
                body=body,
            ))

        else:
            await tab.send(cdp.fetch.continue_response(
                request_id=req_id,
                response_code=status_code,
                response_headers=evt.response_headers,
            ))

    tab.add_handler(cdp.fetch.RequestPaused, _handle_paused)
    await tab.send(cdp.fetch.enable(patterns=[
        cdp.fetch.RequestPattern(
            url_pattern='*c-resume*',
            resource_type=cdp.network.ResourceType.DOCUMENT,
            request_stage=cdp.fetch.RequestStage.RESPONSE,
        ),
        cdp.fetch.RequestPattern(
            url_pattern='*',
            resource_type=cdp.network.ResourceType.IMAGE,
            request_stage=cdp.fetch.RequestStage.RESPONSE,
        ),
    ]))


async def _disable_cors_intercept(tab):
    tab.remove_handlers(cdp.fetch.RequestPaused)
    await tab.send(cdp.fetch.disable())


async def get_resume(tab, idx) -> tuple[str | None, str]:
    await asyncio.sleep(jitter(2))

    # Snapshot existing c-resume frames before clicking so we can identify the new one.
    # The recommend page stacks c-resume iframes rather than reusing them, so the
    # currently visible resume is always the newest frame, not the first one.
    existing_fids = set(await _get_c_resume_frame_ids(tab))

    await _enable_cors_intercept(tab)
    try:
        await _frame_mouse_click_xpath(tab, xpath_resume_card.format(i=idx))

        # Poll until canvas is ready; timeout enforced by asyncio.wait_for() in caller
        while True:
            all_fids = await _get_c_resume_frame_ids(tab)
            new_fids = [f for f in all_fids if f not in existing_fids]
            # Prefer the newly created frame; fall back to the last known one
            fid = new_fids[-1] if new_fids else (all_fids[-1] if all_fids else None)
            if fid:
                canvas_base64 = await _read_canvas_b64(tab, fid)
                if canvas_base64 is not None:
                    break
            await asyncio.sleep(1)
    finally:
        await _disable_cors_intercept(tab)

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
                '//button[starts-with(@class, "btn-v2 btn-sure-v2 btn-greet")]',
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
    await _frame_mouse_click_xpath(tab, xpath_i_know_after_say_hi)


async def close_resume(tab):
    await asyncio.sleep(jitter(1))
    await _frame_mouse_click_xpath(tab, xpath_resume_close)


async def scroll_down(tab):
    await asyncio.sleep(jitter(0.5))
    await _in_frame(tab, "win.scrollTo(0, win.scrollY + 180);")


async def select_job_position(tab, job_title):
    await _frame_mouse_click_css(tab, '.ui-dropmenu-label')
    await asyncio.sleep(jitter(1))

    pos = await tab.evaluate(f"""
        (function() {{
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            var frameRect = frame.getBoundingClientRect();
            var doc = frame.contentDocument;
            if (!doc) return null;
            var options = Array.from(doc.querySelectorAll('ul.job-list li.job-item'));
            for (var opt of options) {{
                if (opt.textContent.trim().startsWith({repr(job_title)})) {{
                    opt.scrollIntoView({{block: 'nearest'}});
                    var r = opt.getBoundingClientRect();
                    return JSON.stringify({{x: frameRect.left + r.left + r.width / 2, y: frameRect.top + r.top + r.height / 2}});
                }}
            }}
            return null;
        }})()
    """)
    if pos:
        coords = json.loads(pos)
        await tab.mouse_click(coords['x'], coords['y'])
        logger.info(f"Selected job: {job_title}")
        await asyncio.sleep(jitter(5))
        await ensure_list_view(tab)
    else:
        logger.warning(f"No job found starting with: {job_title}")


# XPath constants for 新招呼 list (verified via DevTools)
xpath_greeting_items = '//*[contains(@class,"geek-item-wrap")]'
xpath_greeting_unread_badge = './/span[contains(@class,"badge-count")]'
xpath_greeting_job_sub = './/span[@class="source-job"]'
xpath_chat_job_title = '//*[contains(@class,"source-job")]'  # same class used in list items

GREETING_RESUME_LOAD_TIMEOUT = 15  # seconds


async def goto_new_greetings(tab) -> int:
    """Navigate to 沟通 -> 新招呼 tab, then switch to 未读 filter.

    Returns the unread count parsed from the tab label (e.g. '新招呼（3）' -> 3),
    or 0 if no badge is shown.
    """
    await tab.get('https://www.zhipin.com/web/chat/index')
    await asyncio.sleep(jitter(2))
    await dismiss_hover_panels(tab)
    link = await tab.find("新招呼")
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
    unread_tab = await tab.find("未读")
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
    """Click 在线简历 button in the chat panel header.

    Enables Fetch CORS intercept before clicking so the c-resume iframe loads
    with untainted canvas from the start. Caller must ensure get_online_resume_greeting()
    is called afterwards (it disables the intercept on exit).
    """
    # The 在线简历 button sits at ~(1438, 139) which falls inside the interview
    # hover-panel's z-index:100 overlay (1093-1553, 50-205).  Move the mouse
    # to a safe area first so the panel collapses before we try to click.
    await dismiss_hover_panels(tab)
    await _enable_cors_intercept(tab)
    btn = await tab.find("在线简历")
    await btn.click()
    await asyncio.sleep(jitter(2))


async def get_online_resume_greeting(tab) -> tuple[str | None, str]:
    """Wait for canvas resume and return (base64_png, overview_text).

    Disables the Fetch CORS intercept (set up by open_online_resume_greeting) on exit.
    """
    try:
        while True:
            fids = await _get_c_resume_frame_ids(tab)
            if fids:
                canvas_b64 = await _read_canvas_b64(tab, fids[-1])
                if canvas_b64 is not None:
                    break
            await asyncio.sleep(1)
    finally:
        await _disable_cors_intercept(tab)

    overview_text = await tab.evaluate("""
        (function() {
            var s = document.querySelector('.resume-summary');
            return s ? s.innerText.trim() : '';
        })()
    """) or ''
    return canvas_b64, overview_text


async def close_online_resume_greeting(tab):
    """Close the online resume modal via .boss-popup__close (click handler is on the div, not the i)."""
    await asyncio.sleep(jitter(2))
    await _mouse_click_css(tab, '.boss-popup__close')
    # Wait until modal is fully gone before returning — a fixed 1s sleep is not enough
    # when the close animation is slow or the click was missed.
    for _ in range(20):
        still_open = await tab.evaluate("""
            (function() {
                var el = document.querySelector('.boss-popup__close');
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
    send_btn = await tab.find("发送")
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
        clicked = await _mouse_click_css(tab, '.iboss-close')
        if not clicked:
            break
        await asyncio.sleep(0.5)