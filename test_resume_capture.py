"""Local browser verification; run with RUN_BROWSER_TESTS=1. No platform access."""
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from PIL import Image
import zendriver as zd
import driver_utils


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local Chrome test')
class ResumeScreenshotBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_tainted_long_canvas_in_nested_frames(self):
        await self.capture_fixture()

    async def test_parent_scroll_container(self):
        await self.capture_fixture(inner_height=1800, scroll_height=1800)

    async def test_root_window_scroll_is_restored(self):
        await self.capture_fixture(root_offset=2600)

    async def test_parent_hidden_overflow_is_scrolled(self):
        await self.capture_fixture(inner_height=1800, scroll_height=1800,
                                   outer_height=2000, parent_overflow='hidden')

    async def test_parent_unscrollable_clip_is_rejected(self):
        await self.capture_fixture(inner_height=1800, scroll_height=1800,
                                   outer_height=2000, parent_overflow='clip', clipped=True)

    async def test_fractional_canvas_height(self):
        await self.capture_fixture(canvas_height=1799.5)

    async def test_delayed_canvas_paint(self):
        await self.capture_fixture(paint_delay=1200)

    async def test_unrelated_broken_root_image(self):
        await self.capture_fixture(broken_root=True)

    async def test_unrelated_lazy_root_image(self):
        await self.capture_fixture(lazy_root=True)

    async def test_fixed_root_toolbar_is_rejected(self):
        await self.capture_fixture(overlay='root', clipped=True)

    async def test_fixed_parent_toolbar_is_rejected(self):
        await self.capture_fixture(overlay='middle', clipped=True)

    async def test_pointer_transparent_fixed_toolbar_is_rejected(self):
        await self.capture_fixture(overlay='root-transparent', clipped=True)

    async def test_fixed_background_behind_canvas_is_allowed(self):
        await self.capture_fixture(overlay='behind')

    async def test_invisible_fixed_toolbar_is_allowed(self):
        await self.capture_fixture(overlay='invisible')

    async def test_display_none_fixed_toolbar_is_allowed(self):
        await self.capture_fixture(overlay='hidden')

    async def test_busy_state_started_during_final_pass_is_waited_for(self):
        await self.capture_fixture(busy_after_pass=True)

    async def capture_fixture(self, *, inner_height=360, scroll_height=320,
                              outer_height=450, parent_overflow='auto',
                              canvas_height=1800, paint_delay=0, clipped=False, broken_root=False, lazy_root=False, overlay=None, busy_after_pass=False, root_offset=0):
        pixel = io.BytesIO()
        Image.new('RGB', (1, 1), 'white').save(pixel, format='PNG')

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/pixel.png':
                    body = pixel.getvalue()
                    content_type = 'image/png'
                else:
                    content_type = 'text/html'
                    if self.path == '/':
                        html = f'<div style="height:{root_offset}px"></div><iframe src="/middle" style="width:600px;height:{outer_height}px;border:0"></iframe>'
                        if broken_root:
                            html += '<img src="data:image/png;base64,broken">'
                        if lazy_root:
                            html += '<img loading="lazy" src="/pixel.png" style="position:absolute;top:10000px">'
                    elif self.path == '/middle':
                        html = f'<div id="parent-scroll" style="height:320px;overflow:{parent_overflow}"><iframe src="http://127.0.0.1:{other.server_port}/c-resume" style="width:540px;height:{inner_height}px;border:0"></iframe></div>'
                    else:
                        html = f'''<style>body{{margin:0}} #scroll{{height:{scroll_height}px;overflow:auto}}</style>
                        <div id="scroll"><canvas id="resume" width="480" height="1800" style="height:{canvas_height}px"></canvas></div>
                        <script>
                        let c=document.querySelector('canvas'), ctx=c.getContext('2d');
                        let img=new Image(); img.onload=()=>{{
                            ctx.drawImage(img,0,0);
                            const paint=()=>{{for(let i=0;i<6;i++) {{ctx.fillStyle=['red','lime','blue','yellow','magenta','cyan'][i];ctx.fillRect(0,i*300,480,300)}}}};
                            if ({paint_delay}) {{ctx.fillStyle='red';ctx.fillRect(0,0,480,300);setTimeout(paint,{paint_delay})}} else paint();
                            window.ready=true;
                        }}; img.src='http://localhost:{server.server_port}/pixel.png';
                        </script>'''
                    if (overlay in ('root','root-transparent','behind','invisible','hidden') and self.path=='/') or (overlay=='middle' and self.path=='/middle'):
                        pointer_style = 'pointer-events:none;' if overlay in ('root-transparent','behind') else ''
                        extra_style = 'opacity:0;' if overlay=='invisible' else ('display:none;' if overlay=='hidden' else '')
                        z_index = -1 if overlay=='behind' else 9999
                        html += f'<div style="{pointer_style}{extra_style}position:fixed;left:0;top:0;width:100%;height:80px;background:white;z-index:{z_index}">Fixed toolbar</div>'
                    body = html.encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        other = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        for srv in (server, other):
            Thread(target=srv.serve_forever, daemon=True).start()
        browser = None
        try:
            with tempfile.TemporaryDirectory() as profile:
                browser = await zd.start(headless=True, user_data_dir=profile,
                                         browser_args=['--window-size=1000,2400'])
                tab = await browser.get(f'http://127.0.0.1:{server.server_port}/')
                async with asyncio.timeout(15):
                    while not (fids := await driver_utils._get_c_resume_frame_ids(tab)):
                        await asyncio.sleep(0.1)
                    context = await tab.send(driver_utils.cdp.page.create_isolated_world(frame_id=fids[-1]))
                    while not await driver_utils._resume_frame_value(tab, context,
                            "!!document.querySelector('canvas') && document.readyState === 'complete'"):
                        await asyncio.sleep(0.1)
                    await asyncio.sleep(0.2)
                    self.assertTrue(await driver_utils._resume_frame_value(tab, context,
                        "(() => {try {document.querySelector('canvas').toDataURL();return false} catch(e) {return e.name==='SecurityError'}})()"))
                    tree = await tab.send(driver_utils.cdp.page.get_frame_tree())
                    frame_contexts = []
                    def collect_frames(frame):
                        frame_contexts.append(frame.frame.id_)
                        for child in frame.child_frames or []:
                            collect_frames(child)
                    collect_frames(tree)
                    frame_contexts = [await tab.send(driver_utils.cdp.page.create_isolated_world(frame_id=fid))
                                      for fid in frame_contexts]
                    async def scroll_positions():
                        return [await driver_utils._resume_frame_value(tab, ctx, """
                            [scrollX,scrollY,Array.from(document.querySelectorAll('*'))
                                .filter(e=>e.scrollHeight>e.clientHeight || e.scrollWidth>e.clientWidth)
                                .map(e=>[e.tagName,e.id,e.scrollLeft,e.scrollTop])]
                        """) for ctx in frame_contexts]
                    initial_scroll = await scroll_positions()
                    if clipped:
                        with self.assertRaises(driver_utils.ResumeCaptureError):
                            await driver_utils._read_resume_screenshot(tab, fids[-1])
                        self.assertEqual(await scroll_positions(), initial_scroll)
                        return
                    if busy_after_pass:
                        real_capture = driver_utils._capture_resume_strips
                        first_pass = None
                        marked_busy = False
                        async def mark_busy_on_final_pass(*args):
                            nonlocal first_pass, marked_busy
                            image = await real_capture(*args)
                            now = asyncio.get_running_loop().time()
                            if first_pass is None:
                                first_pass = now
                            elif not marked_busy and now-first_pass >= 2:
                                marked_busy = True
                                await driver_utils._resume_frame_value(tab, context, """
                                    document.querySelector('#scroll').setAttribute('aria-busy','true');
                                    setTimeout(()=>document.querySelector('#scroll').removeAttribute('aria-busy'),1000);
                                    true
                                """)
                            return image
                        with patch.object(driver_utils, '_capture_resume_strips', side_effect=mark_busy_on_final_pass):
                            encoded = await driver_utils._read_resume_screenshot(tab, fids[-1])
                        self.assertTrue(marked_busy)
                        self.assertFalse(await driver_utils._resume_frame_value(tab, context,
                            "document.querySelector('#scroll').hasAttribute('aria-busy')"))
                    else:
                        encoded = await driver_utils._read_resume_screenshot(tab, fids[-1])
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as result:
                    self.assertEqual(result.size, (480, 1800))
                    colors = [(255,0,0), (0,255,0), (0,0,255), (255,255,0), (255,0,255), (0,255,255)]
                    rows = range(1800) if canvas_height == 1800 else (i*300+150 for i in range(6))
                    for row in rows:
                        self.assertEqual(result.getpixel((240, row))[:3], colors[row // 300])
                self.assertEqual(await driver_utils._resume_frame_value(tab, context,
                    "document.querySelector('#scroll').scrollTop"), 0)
                self.assertEqual(await scroll_positions(), initial_scroll)
                self.assertTrue(await driver_utils._resume_frame_value(tab, context,
                    "Image.toString().includes('[native code]')"))
                capture_screenshot = driver_utils.cdp.page.capture_screenshot
                shots = 0
                def cancel_second_strip(*args, **kwargs):
                    nonlocal shots
                    shots += 1
                    if shots == 2:
                        raise asyncio.CancelledError()
                    return capture_screenshot(*args, **kwargs)
                with patch.object(driver_utils.cdp.page, 'capture_screenshot', side_effect=cancel_second_strip):
                    with self.assertRaises(asyncio.CancelledError):
                        await driver_utils._read_resume_screenshot(tab, fids[-1])
                self.assertEqual(shots, 2)
                self.assertEqual(await scroll_positions(), initial_scroll)
                self.assertEqual(await driver_utils._resume_frame_value(tab, context,
                    "document.querySelector('#scroll').scrollTop"), 0)
                await driver_utils._resume_frame_value(tab, context,
                    "document.querySelector('#scroll').style.width='400px'; true")
                with self.assertRaises(driver_utils.ResumeCaptureError):
                    await driver_utils._read_resume_screenshot(tab, fids[-1])
                await browser.stop()
                browser = None
        finally:
            if browser:
                await browser.stop()
            for srv in (server, other):
                srv.shutdown()
                srv.server_close()


if __name__ == '__main__':
    unittest.main()
