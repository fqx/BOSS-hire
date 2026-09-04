import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import driver_utils


VERIFY_URL = (
    "https://www.zhipin.com/web/passport/zp/verify.html"
    "?callbackUrl=https%3A%2F%2Fwww.zhipin.com%2Fweb%2Fchat%2Frecommend"
    "&appName=www_zhipin_com_B&code=36"
)


class _RedirectingLink:
    def __init__(self, tab):
        self._tab = tab

    async def click(self):
        self._tab.url = VERIFY_URL


class _RedirectingTab:
    def __init__(self):
        self.url = "https://www.zhipin.com/web/chat/index"

    async def find(self, _text):
        return _RedirectingLink(self)

    async def evaluate(self, _expression):
        return False


class _VerificationTab:
    url = VERIFY_URL

    async def evaluate(self, _expression):
        return None


class _MissingJobTab:
    url = "https://www.zhipin.com/web/chat/recommend"

    async def evaluate(self, _expression):
        return None


class _SelectedJobTab:
    url = "https://www.zhipin.com/web/chat/recommend"

    def __init__(self, selected_label, option_labels=None):
        self.selected_label = selected_label
        self.evaluate = AsyncMock(side_effect=[
            option_labels or [
                "私域电商主播 _ 广州 9-12K",
                "私域电商主播 _ 广州 15-20K 待",
            ],
            '{"x": 100, "y": 200}',
            selected_label,
            None,
        ])
        self.mouse_click = AsyncMock()


class _FrameTreeTab:
    url = "https://www.zhipin.com/web/chat/recommend"

    async def send(self, _command):
        return SimpleNamespace(
            frame=SimpleNamespace(url=self.url),
            child_frames=[
                SimpleNamespace(
                    frame=SimpleNamespace(url=VERIFY_URL),
                    child_frames=None,
                )
            ],
        )


class CaptchaRedirectTests(unittest.IsolatedAsyncioTestCase):
    def test_verify_url_is_recognized(self):
        self.assertTrue(driver_utils._url_is_captcha(VERIFY_URL))

    def test_callback_url_does_not_create_false_positive(self):
        url = (
            "https://www.zhipin.com/web/chat/index"
            "?next=https%3A%2F%2Fwww.zhipin.com%2Fweb%2Fpassport%2Fzp%2Fverify.html"
        )
        self.assertFalse(driver_utils._url_is_captcha(url))

    async def test_nested_verification_frame_is_recognized(self):
        self.assertTrue(await driver_utils._any_frame_has_captcha(_FrameTreeTab()))

    async def test_goto_recommend_detects_redirect_after_click(self):
        tab = _RedirectingTab()

        with patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(driver_utils.CaptchaRequired):
                await driver_utils.goto_recommend(tab)

    async def test_select_job_detects_verification_page_at_entry(self):
        tab = _VerificationTab()

        with patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(driver_utils.CaptchaRequired):
                await driver_utils.select_job_position(tab, "私域电商主播")

    async def test_select_job_does_not_continue_when_position_is_missing(self):
        tab = _MissingJobTab()
        scan_current_job = AsyncMock()

        async def select_then_scan():
            await driver_utils.select_job_position(tab, "私域电商主播")
            await scan_current_job()

        with (
            patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()),
            patch.object(driver_utils, "_any_frame_has_captcha", new=AsyncMock(return_value=False)),
            self.assertRaisesRegex(
                driver_utils.JobPositionNotFound,
                "Expected exactly one job",
            ),
        ):
            await select_then_scan()

        scan_current_job.assert_not_awaited()

    async def test_select_job_confirms_actual_selected_label(self):
        tab = _SelectedJobTab("私域电商主播 _ 广州 9-12K")

        with (
            patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()),
            patch.object(driver_utils, "_any_frame_has_captcha", new=AsyncMock(return_value=False)),
            patch.object(driver_utils, "_frame_mouse_click_css", new=AsyncMock(return_value=True)),
        ):
            await driver_utils.select_job_position(
                tab, "私域电商主播", "私域电商主播 _ 广州 9-12K"
            )

        tab.mouse_click.assert_awaited_once()

    async def test_select_job_rejects_unexpected_actual_label(self):
        tab = _SelectedJobTab("社群运营专员 _ 广州 4-7K")

        with (
            patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()),
            patch.object(driver_utils, "_any_frame_has_captcha", new=AsyncMock(return_value=False)),
            patch.object(driver_utils, "_frame_mouse_click_css", new=AsyncMock(return_value=True)),
            self.assertRaisesRegex(driver_utils.JobPositionNotFound, "selection mismatch"),
        ):
            await driver_utils.select_job_position(
                tab, "私域电商主播", "私域电商主播 _ 广州 9-12K"
            )

    async def test_legacy_select_job_call_still_handles_unique_title(self):
        label = "社群运营专员 _ 广州 4-7K"
        tab = _SelectedJobTab(label, [label])

        with (
            patch.object(driver_utils.asyncio, "sleep", new=AsyncMock()),
            patch.object(driver_utils, "_any_frame_has_captcha", new=AsyncMock(return_value=False)),
            patch.object(driver_utils, "_frame_mouse_click_css", new=AsyncMock(return_value=True)),
        ):
            await driver_utils.select_job_position(tab, "社群运营专员")

        tab.mouse_click.assert_awaited_once()

    def test_select_job_resolves_same_title_by_exact_selector_label(self):
        labels = [
            "私域电商主播 _ 广州 9-12K",
            "私域电商主播 _ 广州 15-20K 待",
        ]
        selected = driver_utils._select_job_option_label(
            labels, "私域电商主播", "私域电商主播 _ 广州 9-12K"
        )
        self.assertEqual(selected, labels[0])

    def test_select_job_rejects_ambiguous_same_title_without_exact_selector(self):
        labels = [
            "私域电商主播 _ 广州 9-12K",
            "私域电商主播 _ 广州 15-20K 待",
        ]
        with self.assertRaisesRegex(
            driver_utils.JobPositionNotFound,
            "Configure selector_job_title.*exact full dropdown label",
        ):
            driver_utils._select_job_option_label(labels, "私域电商主播")

    def test_select_job_rejects_duplicate_exact_selector_labels(self):
        labels = [
            "私域电商主播 _ 广州 9-12K",
            "私域电商主播 _ 广州 9-12K",
        ]
        with self.assertRaises(driver_utils.JobPositionNotFound):
            driver_utils._select_job_option_label(
                labels, "私域电商主播", "私域电商主播 _ 广州 9-12K"
            )


if __name__ == "__main__":
    unittest.main()
