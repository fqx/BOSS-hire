"""Regression tests for stop signals and recoverable resume timeouts."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import driver_utils
import job_utils


class WorkflowErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_propagates_browser_and_verification_errors(self):
        for error in (ConnectionError('disconnected'), driver_utils.CaptchaRequired('verify'),
                      driver_utils.PlatformAccessError('restricted'), ValueError('unexpected')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(driver_utils, 'raise_if_captcha', new=AsyncMock()), \
                 patch.object(driver_utils, 'is_viewed', new=AsyncMock(side_effect=error)), \
                 self.assertRaises(type(error)):
                await job_utils.loop_recommend(None, 2, {}, None, {}, 'test')
        self.assertIsNone(job_utils.logger.handlers[0]._tqdm_instance)

    async def test_verification_check_propagates_disconnection(self):
        tab = SimpleNamespace(url='https://www.zhipin.com/web/chat/index',
                              send=AsyncMock(side_effect=ConnectionError('closed')))
        with self.assertRaises(ConnectionError):
            await driver_utils.raise_if_captcha(tab, 'test')

    async def test_login_redirect_and_restriction_stop_processing(self):
        for url, restricted in [('https://www.zhipin.com/web/user/?intent=1', False),
                                ('https://www.zhipin.com/web/chat/index', True)]:
            tab = SimpleNamespace(url=url, evaluate=AsyncMock(return_value=restricted))
            with patch.object(driver_utils, '_any_frame_has_captcha', new=AsyncMock(return_value=False)), \
                 self.assertRaises(driver_utils.PlatformAccessError):
                await driver_utils.raise_if_captcha(tab, 'test')

    async def test_greeting_action_failure_stops_before_next_candidate(self):
        unread = AsyncMock(return_value=True)
        with patch.object(driver_utils, 'raise_if_captcha', new=AsyncMock()), \
             patch.object(driver_utils, 'is_greeting_unread', new=unread), \
             patch.object(driver_utils, 'get_greeting_job_title_at', new=AsyncMock(return_value='test')), \
             patch.object(driver_utils, 'open_greeting_at', new=AsyncMock()), \
             patch.object(driver_utils, 'get_current_chat_job_title', new=AsyncMock(return_value='test')), \
             patch.object(driver_utils, 'open_online_resume_greeting', new=AsyncMock()), \
             patch.object(driver_utils, 'get_online_resume_greeting', new=AsyncMock(return_value=('png', ''))), \
             patch.object(driver_utils, 'close_online_resume_greeting', new=AsyncMock()), \
             patch.object(job_utils.llm_utils, 'is_qualified_result', return_value=SimpleNamespace(is_qualified=True)), \
             patch.object(driver_utils, 'request_resume', new=AsyncMock(side_effect=ConnectionError('closed'))), \
             self.assertRaises(ConnectionError):
            await job_utils.loop_greetings(None, [{'job_title': 'test'}], None, {})
        unread.assert_awaited_once()

    async def test_recommendation_resume_timeout_closes_before_skipping(self):
        with patch.object(driver_utils, 'raise_if_captcha', new=AsyncMock()), \
             patch.object(driver_utils, 'is_viewed', new=AsyncMock(return_value=False)), \
             patch.object(driver_utils, 'get_age', new=AsyncMock(return_value=28)), \
             patch.object(driver_utils, 'get_resume_card_text', new=AsyncMock(return_value='28岁 5-7K 本科')), \
             patch.object(driver_utils, 'get_resume', new=AsyncMock(side_effect=TimeoutError('slow'))), \
             patch.object(driver_utils, 'close_resume', new=AsyncMock()) as close, \
             patch.object(driver_utils, 'scroll_down', new=AsyncMock()) as scroll, \
             patch.object(driver_utils, 'say_hi', new=AsyncMock()) as greet:
            result = await job_utils.loop_recommend(None, 1, job_utils.get_job_requirements({}), None, {}, 'test')
        self.assertEqual(result, (0, 0))
        close.assert_awaited_once()
        scroll.assert_awaited_once()
        greet.assert_not_awaited()

    async def test_greeting_resume_propagates_unexpected_errors(self):
        for error in (ConnectionError('disconnected'), driver_utils.CaptchaRequired('verify'),
                      driver_utils.PlatformAccessError('restricted'), driver_utils.ResumeCaptureError('clipped')):
            with self.subTest(error=type(error).__name__):
                await self.run_greeting(error, should_raise=True)

    async def test_greeting_timeout_closes_resume_before_continuing(self):
        await self.run_greeting(TimeoutError('slow resume'), should_raise=False)

    async def run_greeting(self, error, should_raise):
        with patch.object(driver_utils, 'raise_if_captcha', new=AsyncMock()), \
             patch.object(driver_utils, 'is_greeting_unread', new=AsyncMock(side_effect=[True, None])), \
             patch.object(driver_utils, 'get_greeting_job_title_at', new=AsyncMock(return_value='test')), \
             patch.object(driver_utils, 'open_greeting_at', new=AsyncMock()), \
             patch.object(driver_utils, 'get_current_chat_job_title', new=AsyncMock(return_value='test')), \
             patch.object(driver_utils, 'open_online_resume_greeting', new=AsyncMock()), \
             patch.object(driver_utils, 'get_online_resume_greeting', new=AsyncMock(side_effect=error)), \
             patch.object(driver_utils, 'close_online_resume_greeting', new=AsyncMock()) as close:
            args = (None, [{'job_title': 'test'}], None, {})
            if should_raise:
                with self.assertRaises(type(error)):
                    await job_utils.loop_greetings(*args)
                close.assert_not_awaited()
            else:
                self.assertEqual(await job_utils.loop_greetings(*args), 0)
                close.assert_awaited_once()


class ClickBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_randomization_stays_in_central_region(self):
        tab = SimpleNamespace(evaluate=AsyncMock(return_value='{"x":100,"y":200,"width":80,"height":20}'),
                              mouse_click=AsyncMock())
        for helper, selector in ((driver_utils._mouse_click_css, '#button'),
                                 (driver_utils._frame_mouse_click_css, '#button'),
                                 (driver_utils._frame_mouse_click_xpath, '//button')):
            for fraction in (-0.2, 0.2):
                with patch.object(driver_utils, 'uniform', return_value=fraction):
                    self.assertTrue(await helper(tab, selector))
                x, y = tab.mouse_click.await_args.args
                self.assertTrue(84 <= x <= 116)
                self.assertTrue(196 <= y <= 204)


if __name__ == '__main__':
    unittest.main()
