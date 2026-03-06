import asyncio
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


async def get_resume(tab, idx) -> str | None:
    await asyncio.sleep(max(2 + gauss(0, 1), 1))
    await _frame_xpath_click(tab, xpath_resume_card.format(i=idx))
    await asyncio.sleep(3)

    # Canvas lives inside a nested c-resume iframe within the recommendFrame
    canvas_base64 = await tab.evaluate("""
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
    return canvas_base64


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