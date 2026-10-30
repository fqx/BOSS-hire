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
                        html = '<iframe src="/middle" style="width:600px;height:450px;border:0"></iframe>'
                    elif self.path == '/middle':
                        html = f'<iframe src="http://127.0.0.1:{other.server_port}/c-resume" style="width:540px;height:360px;border:0"></iframe>'
                    else:
                        html = f'''<style>body{{margin:0}} #scroll{{height:320px;overflow:auto}}</style>
                        <div id="scroll"><canvas id="resume" width="480" height="1800"></canvas></div>
                        <script>
                        let c=document.querySelector('canvas'), ctx=c.getContext('2d');
                        let img=new Image(); img.onload=()=>{{
                            ctx.drawImage(img,0,0);
                            for(let i=0;i<6;i++) {{ctx.fillStyle=['red','lime','blue','yellow','magenta','cyan'][i];ctx.fillRect(0,i*300,480,300)}}
                            window.ready=true;
                        }}; img.src='http://localhost:{server.server_port}/pixel.png';
                        </script>'''
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
                                         browser_args=['--window-size=1000,800'])
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
                    encoded = await driver_utils._read_resume_screenshot(tab, fids[-1])
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as result:
                    self.assertEqual(result.size, (480, 1800))
                    colors = [(255,0,0), (0,255,0), (0,0,255), (255,255,0), (255,0,255), (0,255,255)]
                    for row in range(1800):
                        self.assertEqual(result.getpixel((240, row))[:3], colors[row // 300])
                self.assertEqual(await driver_utils._resume_frame_value(tab, context,
                    "document.querySelector('#scroll').scrollTop"), 0)
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
