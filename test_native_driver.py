"""Transport checks for native automation without interacting with BOSS."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from native_driver import AXNode, NativeDriver, NativeDriverError, NativeStaleElementError


def node(**changes):
    return AXNode(**{
        "path": (0, 2), "role": "AXButton", "title": "", "description": "推荐",
        "value": "", "rect": {"x": 1, "y": 2, "width": 30, "height": 20},
        "actions": ("AXPress",), **changes,
    })


class NativeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_unicode_typing_passes_exact_candidate_and_requires_full_readback(self):
        driver = NativeDriver()
        editor = node(role="AXGenericElement", description="和Ta聊一聊~")
        text = "您好，感谢您的主动联系！简历📄背景符合要求。"
        expected = "牛人分析器，候选人甲，沟通的职位-主播"
        response = {"ok": True, "text": text}
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps(response).encode())) as run:
            self.assertEqual(await driver.type_text(editor, text, expected), text)
        run.assert_awaited_once()
        request = json.loads(run.await_args.args[3])
        self.assertEqual(request, {
            "path": [0, 2], "role": "AXGenericElement", "description": "和Ta聊一聊~",
            "action": "typeText", "text": text, "expected_card_description": expected,
        })

    async def test_typing_rejects_unknown_editors_or_existing_drafts_before_bridge(self):
        for editor in (node(), node(role="AXGenericElement", description="任意标签"),
                       node(role="AXTextArea", value="已有草稿"),
                       node(role="AXStaticText", description="", actions=()),
                       node(role="AXStaticText", description="", value="未授权的草稿")):
            with self.subTest(editor=editor):
                driver = NativeDriver()
                with patch.object(driver, "_prepare", AsyncMock()) as prepare, \
                     self.assertRaises(NativeDriverError):
                    await driver.type_text(editor, "回复文本", "候选人甲")
                prepare.assert_not_awaited()

    async def test_static_text_composer_passes_complete_approved_draft_to_verified_ax_replacement(self):
        draft = "您好，看到您有 3 年以上抖音等多平台电商主播经验，熟悉平播与憋i c"
        editor = node(role="AXStaticText", description="", value=draft[:32], path=(0, 1, 0, 13))
        self.assertNotEqual(editor.value, draft)
        message = "您好，您的电商主播经历符合岗位要求，方便发一份简历进一步沟通吗？"
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps({"ok": True, "text": message}).encode())) as run:
            verified = await driver.type_text(editor, message, "当前候选人会话卡片", replace_draft=draft)
        self.assertEqual(verified, message)
        run.assert_awaited_once()
        self.assertEqual(json.loads(run.await_args.args[3]), {
            "path": [0, 1, 0, 13], "role": "AXStaticText", "description": "", "action": "typeText",
            "text": message, "expected_card_description": "当前候选人会话卡片", "expected_draft": draft,
        })

    async def test_typing_replaces_only_an_explicitly_approved_exact_draft(self):
        editor = node(role="AXTextArea", description="", value="da")
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps({"ok": True, "text": "回复文本"}).encode())) as run:
            self.assertEqual(await driver.type_text(editor, "回复文本", "候选人甲", replace_draft="da"), "回复文本")
        self.assertEqual(json.loads(run.await_args.args[3])["expected_draft"], "da")
        with patch.object(driver, "_prepare", AsyncMock()) as prepare, \
             self.assertRaisesRegex(NativeDriverError, "draft"):
            await driver.type_text(editor, "回复文本", "候选人甲")
        prepare.assert_not_awaited()
        for approved in ("different draft", ""):
            with self.subTest(approved=approved), \
                 patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
                 patch.object(driver, "_run", AsyncMock(side_effect=NativeDriverError("Complete draft does not match approved replacement"))) as run, \
                 self.assertRaisesRegex(NativeDriverError, "draft"):
                await driver.type_text(editor, "回复文本", "候选人甲", replace_draft=approved)
            run.assert_awaited_once()
            self.assertEqual(json.loads(run.await_args.args[3])["expected_draft"], approved)

    async def test_typing_replacement_permission_is_not_sent_by_default(self):
        driver = NativeDriver()
        editor = node(role="AXGenericElement", description="和Ta聊一聊~")
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps({"ok": True, "text": "回复文本"}).encode())) as run:
            await driver.type_text(editor, "回复文本", "候选人甲")
        self.assertNotIn("expected_draft", json.loads(run.await_args.args[3]))

    async def test_typing_rejects_submission_keys_and_missing_candidate_identity(self):
        editor = node(role="AXGenericElement", description="和Ta聊一聊~")
        for text, identity in (("", "甲"), ("文字\n", "甲"), ("文字\r", "甲"),
                               ("文字\t", "甲"), ("文字\x00", "甲"),
                               ("文字\u2028", "甲"), ("文字", "")):
            with self.subTest(text=text, identity=identity):
                driver = NativeDriver()
                with patch.object(driver, "_prepare", AsyncMock()) as prepare, \
                     self.assertRaises(ValueError):
                    await driver.type_text(editor, text, identity)
                prepare.assert_not_awaited()

    async def test_typing_readback_mismatch_or_focus_failure_is_not_retried(self):
        editor = node(role="AXGenericElement", description="和Ta聊一聊~")
        for response in ({"ok": True, "text": "错误文字"}, {"ok": True}):
            with self.subTest(response=response):
                driver = NativeDriver()
                with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
                     patch.object(driver, "_run", AsyncMock(return_value=json.dumps(response).encode())) as run, \
                     self.assertRaisesRegex(NativeDriverError, "verify the complete"):
                    await driver.type_text(editor, "回复文本", "候选人甲")
                run.assert_awaited_once()
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(side_effect=NativeDriverError("Focused editor already contains a draft"))) as run, \
             self.assertRaisesRegex(NativeDriverError, "draft"):
            await driver.type_text(editor, "回复文本", "候选人甲")
        run.assert_awaited_once()

    async def test_window_info_preserves_matched_window_id_and_bounds(self):
        info = {"window_id": 2163, "bounds": {"x": 11, "y": 30, "width": 699, "height": 965}}
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps(info).encode())) as run:
            self.assertEqual(await driver.window_info(), info)
        run.assert_awaited_once_with("bridge", "window-info", "com.hpbr.bosszhipin")

    async def test_window_info_rejects_invalid_geometry_and_ambiguous_match(self):
        for info in (
            {"window_id": True, "bounds": {"x": 0, "y": 0, "width": 100, "height": 100}},
            {"window_id": 2, "bounds": {"x": 0, "y": 0, "width": float("inf"), "height": 100}},
            {"window_id": 2, "bounds": {"x": 0, "y": 0, "width": 0, "height": 100}},
        ):
            with self.subTest(info=info):
                driver = NativeDriver()
                with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
                     patch.object(driver, "_run", AsyncMock(return_value=json.dumps(info).encode())), \
                     self.assertRaisesRegex(NativeDriverError, "invalid window metadata"):
                    await driver.window_info()
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(side_effect=NativeDriverError("window match found 2"))), \
             self.assertRaisesRegex(NativeDriverError, "found 2"):
            await driver.window_info()

    async def test_optional_capture_uses_matched_id_and_cleans_temporary_png(self):
        info = {"window_id": 2163, "bounds": {"x": 11, "y": 30, "width": 699, "height": 965}}
        png = b"\x89PNG\r\n\x1a\nlocal test image bytes"
        outputs = []
        async def capture(*args):
            self.assertEqual(args[:5], ("/usr/sbin/screencapture", "-x", "-o", "-l", 2163))
            output = Path(args[-1])
            outputs.append(output)
            output.write_bytes(png)
            return b""
        driver = NativeDriver()
        with patch.object(driver, "window_info", AsyncMock(side_effect=[info, info])), \
             patch.object(driver, "_run", AsyncMock(side_effect=capture)):
            self.assertEqual(await driver.capture_window(), (png, info["bounds"]))
        self.assertFalse(outputs[0].parent.exists())

    async def test_capture_denial_and_cancellation_remove_partial_outputs(self):
        info = {"window_id": 2163, "bounds": {"x": 11, "y": 30, "width": 699, "height": 965}}
        for error in (NativeDriverError("capture denied"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                outputs = []
                async def capture(*args):
                    output = Path(args[-1])
                    outputs.append(output)
                    output.write_bytes(b"partial")
                    raise error
                driver = NativeDriver()
                with patch.object(driver, "window_info", AsyncMock(return_value=info)), \
                     patch.object(driver, "_run", AsyncMock(side_effect=capture)):
                    if isinstance(error, NativeDriverError):
                        with self.assertRaisesRegex(NativeDriverError, "屏幕录制权限"):
                            await driver.capture_window()
                    else:
                        with self.assertRaises(asyncio.CancelledError):
                            await driver.capture_window()
                self.assertFalse(outputs[0].parent.exists())

    async def test_capture_rejects_window_changes_before_returning_image(self):
        info = {"window_id": 2163, "bounds": {"x": 11, "y": 30, "width": 699, "height": 965}}
        for after in ({**info, "window_id": 2164},
                      {**info, "bounds": {**info["bounds"], "x": 12}},
                      {**info, "bounds": {**info["bounds"], "height": 966}}):
            with self.subTest(after=after):
                outputs = []
                async def capture(*args):
                    output = Path(args[-1])
                    outputs.append(output)
                    output.write_bytes(b"\x89PNG\r\n\x1a\nimage")
                    return b""
                driver = NativeDriver()
                with patch.object(driver, "window_info", AsyncMock(side_effect=[info, after])), \
                     patch.object(driver, "_run", AsyncMock(side_effect=capture)), \
                     self.assertRaisesRegex(NativeDriverError, "位置尺寸发生变化"):
                    await driver.capture_window()
                self.assertFalse(outputs[0].parent.exists())

    async def test_snapshot_preserves_text_paths_and_actions(self):
        driver = NativeDriver()
        payload = {
            "path": [0, 2], "role": "AXButton", "title": "", "description": "推荐",
            "value": "选中", "rect": {"x": 1, "y": 2, "width": 30, "height": 20},
            "actions": ["AXPress"],
        }
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps([payload]).encode())):
            result = await driver.snapshot()
        self.assertEqual(result, [node(value="选中")])

    async def test_malformed_snapshot_stops_instead_of_partial_data(self):
        for payload in ({"error": "permission"}, [{"path": [0]}]):
            with self.subTest(payload=payload):
                driver = NativeDriver()
                with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
                     patch.object(driver, "_run", AsyncMock(return_value=json.dumps(payload).encode())), \
                     self.assertRaises(NativeDriverError):
                    await driver.snapshot()

    async def test_snapshot_rejects_nonfinite_geometry_at_transport_boundary(self):
        valid = {
            "path": [0, 2], "role": "AXStaticText", "title": "", "description": "完整工作经历",
            "value": "", "rect": {"x": 1, "y": 2, "width": 30, "height": 20},
            "actions": [],
        }
        for key in ("x", "y", "width", "height"):
            for value in (float("inf"), float("-inf"), float("nan")):
                with self.subTest(key=key, value=value):
                    payload = {**valid, "rect": {**valid["rect"], key: value}}
                    driver = NativeDriver()
                    with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
                         patch.object(driver, "_run", AsyncMock(return_value=json.dumps([payload]).encode())), \
                         self.assertRaisesRegex(NativeDriverError, "invalid node"):
                        await driver.snapshot()

    async def test_zero_unavailable_geometry_preserves_resume_text(self):
        payload = {
            "path": [0, 2], "role": "AXStaticText", "title": "", "description": "完整工作经历",
            "value": "", "rect": dict.fromkeys(("x", "y", "width", "height"), 0),
            "actions": [],
        }
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=json.dumps([payload]).encode())):
            result = await driver.snapshot()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].description, "完整工作经历")
        self.assertEqual(result[0].rect["height"], 0)

    async def test_action_carries_identity_for_native_stale_element_check(self):
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=b'{"ok":true}')) as run:
            await driver.perform(node(), "press")
        args = run.await_args.args
        self.assertEqual(args[:3], ("bridge", "action", "com.hpbr.bosszhipin"))
        self.assertEqual(json.loads(args[3]), {
            "path": [0, 2], "role": "AXButton", "description": "推荐", "action": "AXPress",
        })

    async def test_stale_element_failure_propagates(self):
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(side_effect=NativeDriverError("Stale accessibility element"))), \
             self.assertRaisesRegex(NativeDriverError, "Stale"):
            await driver.perform(node(), "press")

    async def test_unsupported_action_never_launches_bridge(self):
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock()) as prepare, \
             self.assertRaisesRegex(NativeDriverError, "does not support"):
            await driver.perform(node(), "scrollDown")
        prepare.assert_not_awaited()

    async def test_text_update_only_accepts_text_controls(self):
        driver = NativeDriver()
        with self.assertRaisesRegex(NativeDriverError, "text field"):
            await driver.set_value(node(), "hello")
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch.object(driver, "_run", AsyncMock(return_value=b'{"ok":true}')) as run:
            await driver.set_value(node(role="AXTextArea"), "测试文字")
        self.assertEqual(json.loads(run.await_args.args[3])["text"], "测试文字")

    async def test_process_error_is_not_swallowed(self):
        process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", b"Accessibility permission is required")))
        with patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
             self.assertRaisesRegex(NativeDriverError, "permission"):
            await NativeDriver()._run("bridge")

    async def test_exact_pre_action_stale_errors_have_a_distinct_type(self):
        for error in ("Stale accessibility path", "Stale accessibility element: role or description changed"):
            stderr = json.dumps({"error": error})
            process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", stderr.encode())))
            with self.subTest(error=error), \
                 patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as start, \
                 self.assertRaises(NativeStaleElementError) as raised:
                await NativeDriver()._run("bridge", "action")
            self.assertEqual(str(raised.exception), stderr)
            start.assert_awaited_once()
            process.communicate.assert_awaited_once()

    async def test_other_process_errors_preserve_base_type_and_original_stderr(self):
        errors = (
            "AccessibilityBridge.swift:10: error: unexpected token\ncompiler diagnostics",
            '{"error":"AXPress failed after submission"}',
            '{"error":"Stale accessibility path after action"}',
            '{"error":"Stale accessibility element"}',
            '"Stale accessibility path"',
            '["Stale accessibility path"]',
            '{"error":null}',
            "{invalid JSON",
        )
        for stderr in errors:
            process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", stderr.encode())))
            with self.subTest(stderr=stderr), \
                 patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
                 self.assertRaises(NativeDriverError) as raised:
                await NativeDriver()._run("bridge")
            self.assertIs(type(raised.exception), NativeDriverError)
            self.assertEqual(str(raised.exception), stderr)

    async def test_action_propagates_pre_action_stale_type_without_retrying(self):
        process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", b'{"error":"Stale accessibility path"}')))
        driver = NativeDriver()
        with patch.object(driver, "_prepare", AsyncMock(return_value="bridge")), \
             patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as start, \
             self.assertRaises(NativeStaleElementError):
            await driver.perform(node(), "press")
        start.assert_awaited_once()
        process.communicate.assert_awaited_once()

    async def test_cancel_kills_and_reaps_process(self):
        entered = asyncio.Event()
        async def communicate():
            entered.set()
            await asyncio.Future()
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=communicate)
        with patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(NativeDriver()._run("bridge"))
            await entered.wait()
            process.communicate.side_effect = None
            process.communicate.return_value = (b"", b"")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    async def test_timeout_kills_and_reaps_process(self):
        process = Mock(returncode=None)
        async def communicate():
            if process.communicate.await_count == 1:
                await asyncio.Future()
            return b"", b""
        process.communicate = AsyncMock(side_effect=communicate)
        with patch("native_driver.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
             self.assertRaises(TimeoutError):
            await NativeDriver(timeout=0.001)._run("bridge")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
