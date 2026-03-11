import asyncio
import json
from log_utils import logger
import re
from random import gauss



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


async def log_in(tab):
    await tab.get('https://www.zhipin.com/web/user/?intent=1')
    await asyncio.sleep(3)
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
    await asyncio.sleep(3)


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
    await asyncio.sleep(2)


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
        await asyncio.sleep(3)
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


async def _get_canvas_base64(tab):
    return await tab.evaluate("""
        (function() {
            var frame = document.querySelector('iframe[name="recommendFrame"]');
            if (!frame) return null;
            try {
                var doc = frame.contentDocument;
                var cFrame = doc.querySelector('iframe[src*="c-resume"]');
                if (cFrame) {
                    var canvas = cFrame.contentDocument.querySelector('canvas#resume');
                    if (canvas) return canvas.toDataURL('image/png').substring(22);
                }
                // fallback: canvas directly in recommend frame
                var canvas = doc.querySelector('canvas#resume');
                if (canvas) return canvas.toDataURL('image/png').substring(22);
            } catch(e) {}
            return null;
        })()
    """)


async def get_resume(tab, idx) -> tuple[str | None, str]:
    await asyncio.sleep(max(2 + gauss(0, 1), 1))
    await _frame_xpath_click(tab, xpath_resume_card.format(i=idx))

    # Poll until canvas appears; overall timeout is enforced by asyncio.wait_for() in the caller
    while True:
        canvas_base64 = await _get_canvas_base64(tab)
        if canvas_base64 is not None:
            break
        await asyncio.sleep(1)

    # Extract the "经历概览" sidebar text from the recommendFrame DOM
    overview_text = await _in_frame(tab, """
        var summary = doc.querySelector('.resume-summary');
        return summary ? summary.innerText.trim() : '';
    """) or ''

    return canvas_base64, overview_text


async def say_hi(tab):
    await asyncio.sleep(1)
    await _frame_xpath_click(tab, xpath_say_hi)
    await asyncio.sleep(1)
    try:
        await _frame_xpath_click(tab, xpath_i_know_after_say_hi)
    except Exception:
        pass


async def close_resume(tab):
    await asyncio.sleep(1)
    await _frame_xpath_click(tab, xpath_resume_close)


async def scroll_down(tab):
    await asyncio.sleep(0.5)
    await _in_frame(tab, "win.scrollTo(0, win.scrollY + 180);")


async def select_job_position(tab, job_title):
    await _in_frame(tab, """
        var dropdown = doc.querySelector('.ui-dropmenu-label');
        if (dropdown) dropdown.click();
    """)
    await asyncio.sleep(1)

    found = await _in_frame(tab, f"""
        var options = Array.from(doc.querySelectorAll('ul.job-list li.job-item'));
        for (var opt of options) {{
            if (opt.textContent.trim().startsWith({repr(job_title)})) {{
                opt.click();
                return true;
            }}
        }}
        return false;
    """)
    if found:
        logger.info(f"Selected job: {job_title}")
        await asyncio.sleep(5)
    else:
        logger.warning(f"No job found starting with: {job_title}")


# XPath constants for 新招呼 list (verified via DevTools)
xpath_greeting_items = '//*[contains(@class,"geek-item-wrap")]'
xpath_greeting_unread_badge = './/span[contains(@class,"badge-count")]'
xpath_greeting_job_sub = './/span[@class="source-job"]'
xpath_chat_job_title = '//*[contains(@class,"source-job")]'  # same class used in list items

GREETING_RESUME_LOAD_TIMEOUT = 15  # seconds


async def goto_new_greetings(tab):
    """Navigate to 沟通 -> 新招呼 tab, then switch to 未读 filter."""
    await tab.get('https://www.zhipin.com/web/chat/index')
    await asyncio.sleep(2)
    link = await tab.find("新招呼")
    await link.click()
    await asyncio.sleep(2)
    # Click 未读 to show only unread candidates
    unread_tab = await tab.find("未读")
    await unread_tab.click()
    await asyncio.sleep(1)


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
    await asyncio.sleep(1.5)


async def get_current_chat_job_title(tab) -> str:
    """Read the job title from the active chat panel (same source-job class as list)."""
    return await tab.evaluate("""
        (function() {
            var el = document.querySelector('.source-job');
            return el ? el.innerText.trim() : '';
        })()
    """) or ''


async def open_online_resume_greeting(tab):
    """Click 在线简历 button in the chat panel header."""
    btn = await tab.find("在线简历")
    await btn.click()
    await asyncio.sleep(2)


async def get_online_resume_greeting(tab) -> tuple[str | None, str]:
    """Wait for canvas resume and return (base64_png, overview_text)."""
    while True:
        canvas_b64 = await tab.evaluate("""
            (function() {
                var f = document.querySelector('iframe[src*="c-resume"]');
                if (f) {
                    try {
                        var c = f.contentDocument.querySelector('canvas#resume');
                        if (c) return c.toDataURL('image/png').substring(22);
                    } catch(e) {}
                }
                var c = document.querySelector('canvas#resume');
                return c ? c.toDataURL('image/png').substring(22) : null;
            })()
        """)
        if canvas_b64 is not None:
            break
        await asyncio.sleep(1)

    overview_text = await tab.evaluate("""
        (function() {
            var s = document.querySelector('.resume-summary');
            return s ? s.innerText.trim() : '';
        })()
    """) or ''
    return canvas_b64, overview_text


async def close_online_resume_greeting(tab):
    """Close the online resume modal via .boss-popup__close (click handler is on the div, not the i)."""
    await asyncio.sleep(max(2 + gauss(0, 1), 1))
    await tab.evaluate("""
        (function() {
            var btn = document.querySelector('.boss-popup__close');
            if (btn) { btn.click(); return true; }
            return false;
        })()
    """)
    await asyncio.sleep(1)


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
    await asyncio.sleep(0.5)
    send_btn = await tab.find("发送")
    await send_btn.click()
    await asyncio.sleep(1)


async def request_resume(tab):
    """Send greeting message to qualified candidate (求简历 requires mutual reply first)."""
    await send_chat_message(tab, GREETING_QUALIFIED_MSG)


async def mark_unsuitable(tab, reason_category: str):
    """Click 不合适 button, then click the matching preset reason in the dialog."""
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
    await asyncio.sleep(2)

    # Wait for reason-list to populate (renders async after dialog opens)
    for _ in range(10):
        count = await tab.evaluate("document.querySelector('.reason-list')?.children.length || 0")
        if count:
            break
        await asyncio.sleep(0.5)

    # Click reason item via JS .click() — element is in a modal so coordinates are unreliable
    escaped = reason_category.replace('"', '\\"')
    clicked_reason = await tab.evaluate(f"""
        (function() {{
            var result = document.evaluate(
                '//*[contains(@class,"reason-item") and normalize-space()="{escaped}"]',
                document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            );
            var node = result.singleNodeValue;
            if (node) {{ node.click(); return node.outerHTML.substring(0, 100); }}
            return null;
        }})()
    """)
    if not clicked_reason:
        raise RuntimeError(f"Could not find reason button: {reason_category}")
    await asyncio.sleep(1)

    # Confirm if a confirm button appears
    confirmed = await tab.evaluate("""
        (function() {
            var all = document.querySelectorAll('*');
            for (var el of all) {
                if (el.children.length === 0 && el.innerText && el.innerText.trim() === '确定') {
                    el.click(); return true;
                }
            }
            return false;
        })()
    """)
    await asyncio.sleep(1)


async def close_popover(tab):
    while True:
        closed = await tab.evaluate("""
            (function() {
                var btn = document.querySelector('.iboss-close');
                if (btn) { btn.click(); return true; }
                return false;
            })()
        """)
        if not closed:
            break
        await asyncio.sleep(0.5)