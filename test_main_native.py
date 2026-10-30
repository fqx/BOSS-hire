"""Native entrypoint routing and separate contact/rejection review controls."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
import native_workflow
from native_driver import NativeDailyLimitError


class NativeEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_review_logs_complete_reason_and_requires_explicit_yes(self):
        result = SimpleNamespace(reason_category="过往经历不符", reason="候选人姓名：测试候选人\n" + "完整经历分析及淘汰理由。" * 20 + "\n理由结尾。")
        for answer in ("", " ", "n", "no", "确认", "y", " Y ", "yes"):
            with self.subTest(answer=answer):
                with patch("builtins.input", return_value=answer) as prompt, \
                     patch.object(main.log_utils.logger, "llm") as output:
                    approved = await main.review_rejection("主播", result)
                self.assertEqual(approved, answer.strip().lower() in ("y", "yes"))
                self.assertIn(result.reason, output.call_args.args[0])
                self.assertIn(result.reason_category, output.call_args.args[0])
                self.assertIn("主播", output.call_args.args[0])
                self.assertIn("[y/N]", prompt.call_args.args[0])

    async def test_rejection_review_eof_does_not_approve(self):
        with patch("builtins.input", side_effect=EOFError), \
             patch.object(main.log_utils.logger, "llm"):
            self.assertFalse(await main.review_rejection("主播", SimpleNamespace(reason="完整理由", reason_category="其他原因")))

    async def test_explicit_review_mode_installs_per_candidate_rejection_reviewer(self):
        args = SimpleNamespace(backend="native", allow_contact=True, config=None, review_rejections=True)
        configs = [{"job_title": "市场专员", "job_requirements": {}}]
        with patch.object(main, "get_arguments", return_value=args), \
             patch.object(main, "get_params", return_value=configs), \
             patch.object(main, "OpenAI", return_value="client"), \
             patch.object(main.wakelock_utils, "WakeLock", return_value=nullcontext()), \
             patch.object(native_workflow, "run_native", new=AsyncMock()) as run, \
             patch.object(main, "log_final_stats"):
            await main.main()
        self.assertIs(run.await_args.kwargs["rejection_reviewer"], main.review_rejection)
        self.assertTrue(run.await_args.kwargs["allow_contact"])

    async def test_run_native_forwards_reviewer_to_inbound_loop(self):
        reviewer = AsyncMock(return_value=False)
        driver = object()
        with patch("native_greetings.loop_greetings", new=AsyncMock()) as loop:
            await native_workflow.run_native([], "client", {}, driver=driver,
                                             allow_contact=True, recommend=False, rejection_reviewer=reviewer)
        self.assertIs(loop.await_args.kwargs["rejection_reviewer"], reviewer)
        self.assertIs(loop.await_args.args[0].driver, driver)

    def test_cli_defaults_to_native_contact_without_individual_prompts(self):
        with patch("sys.argv", ["main.py"]):
            args = main.get_arguments()
        self.assertEqual(args.backend, "native")
        self.assertTrue(args.allow_contact)
        self.assertFalse(args.review_rejections)

    def test_cli_read_only_disables_contact_and_allow_contact_remains_alias(self):
        with patch("sys.argv", ["main.py", "--read-only"]):
            self.assertFalse(main.get_arguments().allow_contact)
        with patch("sys.argv", ["main.py", "--allow-contact"]):
            self.assertTrue(main.get_arguments().allow_contact)
        with patch("sys.argv", ["main.py", "--review-rejections"]):
            self.assertTrue(main.get_arguments().review_rejections)

    def test_cli_rejects_unsupported_read_only_or_conflicting_review_flags(self):
        for flags in (["--backend", "web", "--read-only"],
                      ["--read-only", "--review-rejections"],
                      ["--backend", "web", "--review-rejections"]):
            with self.subTest(flags=flags), patch("sys.argv", ["main.py", *flags]), \
                 patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
                main.get_arguments()
            self.assertEqual(raised.exception.code, 2)

    async def test_default_contact_mode_does_not_install_interactive_reviewer(self):
        args = SimpleNamespace(backend="native", allow_contact=True, config=None, review_rejections=False)
        with patch.object(main, "get_arguments", return_value=args), \
             patch.object(main, "get_params", return_value=[{"job_title": "主播", "job_requirements": {}}]), \
             patch.object(main, "OpenAI", return_value="client"), \
             patch.object(main.wakelock_utils, "WakeLock", return_value=nullcontext()), \
             patch.object(native_workflow, "run_native", new=AsyncMock()) as run, \
             patch.object(main, "log_final_stats"):
            await main.main()
        self.assertTrue(run.await_args.kwargs["allow_contact"])
        self.assertNotIn("rejection_reviewer", run.await_args.kwargs)

    def test_local_model_client_bypasses_system_proxy(self):
        with patch.object(main, "OPENAI_BASE_URL", "http://localhost:8504/v1"), \
             patch.object(main, "OpenAI") as client, \
             patch.object(main.httpx, "Client") as http_client:
            result = main.create_openai_client()
        http_client.assert_called_once_with(trust_env=False)
        self.assertIs(result, client.return_value)
        self.assertIs(client.call_args.kwargs["http_client"], http_client.return_value)
        self.assertEqual(client.call_args.kwargs["base_url"], "http://localhost:8504/v1")

    def test_cloud_model_client_keeps_default_proxy_handling(self):
        with patch.object(main, "OPENAI_BASE_URL", "https://api.openai.com/v1"), \
             patch.object(main, "OpenAI") as client, \
             patch.object(main.httpx, "Client") as http_client:
            main.create_openai_client()
        http_client.assert_not_called()
        self.assertNotIn("http_client", client.call_args.kwargs)

    async def test_native_mode_routes_flags_and_does_not_launch_browser(self):
        args = SimpleNamespace(backend="native", allow_contact=False, config=None, review_rejections=False)
        configs = [{"job_title": "市场专员", "job_requirements": {}}]
        with patch.object(main, "get_arguments", return_value=args), \
             patch.object(main, "get_params", return_value=configs), \
             patch.object(main, "OpenAI", return_value="client"), \
             patch.object(main.wakelock_utils, "WakeLock", return_value=nullcontext()), \
             patch.object(main, "launch_browser", new=AsyncMock()) as launch, \
             patch.object(native_workflow, "run_native", new=AsyncMock()) as run, \
             patch.object(main, "log_final_stats") as final:
            await main.main()
        launch.assert_not_awaited()
        run.assert_awaited_once_with(
            configs, "client", main.job_stats, allow_contact=False,
            greetings=main.ENABLE_GREETINGS_LOOP, recommend=main.ENABLE_RECOMMEND_LOOP,
        )
        final.assert_called_once()

    async def test_daily_limit_stops_cleanly_and_reports_statistics(self):
        args = SimpleNamespace(backend="native", allow_contact=True, config=None, review_rejections=False)
        with patch.object(main, "get_arguments", return_value=args), \
             patch.object(main, "get_params", return_value=[{"job_title": "主播", "job_requirements": {}}]), \
             patch.object(main, "create_openai_client", return_value="client"), \
             patch.object(main.wakelock_utils, "WakeLock", return_value=nullcontext()), \
             patch.object(native_workflow, "run_native",
                          new=AsyncMock(side_effect=NativeDailyLimitError("limit"))), \
             patch.object(main, "log_final_stats") as final, \
             patch.object(main.log_utils.logger, "warning") as warning:
            await main.main()
        final.assert_called_once()
        self.assertIn("每日查看/沟通上限", warning.call_args.args[0])

    async def test_native_errors_propagate_and_still_report_statistics(self):
        args = SimpleNamespace(backend="native", allow_contact=False, config=None, review_rejections=False)
        with patch.object(main, "get_arguments", return_value=args), \
             patch.object(main, "get_params", return_value=[{"job_title": "test"}]), \
             patch.object(main, "OpenAI", return_value="client"), \
             patch.object(main.wakelock_utils, "WakeLock", return_value=nullcontext()), \
             patch.object(native_workflow, "run_native", new=AsyncMock(side_effect=ConnectionError("lost"))), \
             patch.object(main, "log_final_stats") as final, \
             self.assertRaises(ConnectionError):
            await main.main()
        final.assert_called_once()

    def test_inbound_only_statistics_are_reported(self):
        with patch.object(main, "job_stats", {"主播": {"inbound_viewed": 2, "inbound_qualified": 1}}), \
             patch.object(main.log_utils.logger, "llm", new=Mock()) as output:
            main.log_final_stats()
        self.assertIn("新招呼查看数 2", output.call_args.args[0])
        self.assertIn("新招呼符合人数 1", output.call_args.args[0])

    def test_card_prefilter_statistics_separate_skips_from_detail_views(self):
        with patch.object(main, "job_stats", {"销售": {"viewed": 0, "card_prefilter_skipped": 2}}), \
             patch.object(main.log_utils.logger, "llm", new=Mock()) as output:
            main.log_final_stats()
        self.assertIn("简历查看数 0", output.call_args.args[0])
        self.assertIn("卡片关键词初筛跳过数 2", output.call_args.args[0])

    def test_pending_rejections_report_complete_reason_without_marking(self):
        stats = {"主播": {"awaiting_review": 1, "pending_rejections": [{
            "reason_category": "过往经历不符", "reason": "候选人姓名：测试\n完整淘汰建议。",
            "expected_card_description": "待复核候选人卡片",
        }]}}
        with patch.object(main, "job_stats", stats), \
             patch.object(main.log_utils.logger, "llm") as output:
            main.log_final_stats()
        logged = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("待人工复核数 1", logged)
        self.assertIn("完整淘汰建议。", logged)
        self.assertIn("过往经历不符", logged)


if __name__ == "__main__":
    unittest.main()
