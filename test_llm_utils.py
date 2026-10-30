import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from pydantic import ValidationError

import llm_utils


class InterviewerSchemaTests(unittest.TestCase):
    @staticmethod
    def _client_returning(content):
        message = SimpleNamespace(content=content, model_extra={})
        completions = SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                choices=[SimpleNamespace(message=message)]
            )
        )
        return SimpleNamespace(chat=SimpleNamespace(completions=completions))

    def test_fields_put_boolean_after_reason_and_category(self):
        properties = llm_utils.interviewer.model_json_schema()["properties"]
        self.assertEqual(
            list(properties),
            ["reason", "reason_category", "is_qualified"],
        )

    def test_qualified_result_requires_empty_category(self):
        result = llm_utils.interviewer(
            reason="候选人相关经验满足要求。\n最终结论：符合",
            reason_category="",
            is_qualified=True,
        )
        self.assertEqual(result.reason_category, "")

        with self.assertRaises(ValidationError):
            llm_utils.interviewer(
                reason="候选人相关经验满足要求。\n最终结论：符合",
                reason_category="其他原因",
                is_qualified=True,
            )

    def test_unqualified_result_requires_one_of_the_valid_categories(self):
        for category in llm_utils.DISQUALIFICATION_REASON_CATEGORIES:
            with self.subTest(category=category):
                result = llm_utils.interviewer(
                    reason="候选人缺少相关经验。\n最终结论：不符合",
                    reason_category=category,
                    is_qualified=False,
                )
                self.assertEqual(result.reason_category, category)

        for category in ("", "不存在的原因"):
            with self.subTest(category=category), self.assertRaises(ValidationError):
                llm_utils.interviewer(
                    reason="候选人缺少相关经验。\n最终结论：不符合",
                    reason_category=category,
                    is_qualified=False,
                )

    def test_parse_accepts_unqualified_result_without_final_marker(self):
        content = (
            '{"reason":"候选人姓名：谢薇\\n候选人缺少相关经验。",'
            '"reason_category":"过往经历不符","is_qualified":false}'
        )
        client = self._client_returning(content)
        with patch.object(llm_utils, "_is_openai_cloud", False):
            result = llm_utils.is_qualified_result(client, "image", "requirements")
        self.assertIsNotNone(result)
        self.assertFalse(result.is_qualified)
        self.assertTrue(result.reason.startswith("候选人姓名：谢薇"))

    def test_boolean_is_authoritative_over_legacy_marker(self):
        result = llm_utils.interviewer(
            reason="候选人姓名：测试\n最终结论：不符合",
            reason_category="",
            is_qualified=True,
        )
        self.assertTrue(result.is_qualified)

    def test_qualified_result_without_final_marker(self):
        result = llm_utils._parse_content(
            '{"reason":"候选人姓名：测试，相关经验满足要求。",'
            '"reason_category":"","is_qualified":true}'
        )
        self.assertTrue(result.is_qualified)


class LLMRetryTimingTests(unittest.TestCase):
    def test_retry_budget_and_error_result_unchanged_with_jittered_delays(self):
        import httpx
        error = llm_utils.openai.APIConnectionError(request=httpx.Request("POST", "http://localhost/test"))
        for cloud in (True, False):
            endpoint = "_call_responses_api" if cloud else "_call_chat_api"
            with self.subTest(cloud=cloud), \
                 patch.object(llm_utils, "_is_openai_cloud", cloud), \
                 patch.object(llm_utils, endpoint, side_effect=error) as request, \
                 patch.object(llm_utils, "jitter", side_effect=[7.5, 24.0]) as jitter, \
                 patch.object(llm_utils.time, "sleep") as sleep, \
                 patch.object(llm_utils.logger, "warning") as warning:
                self.assertIsNone(llm_utils._call_llm(None, "image", "requirements", "overview"))
            self.assertEqual(request.call_count, len(llm_utils.RETRY_DELAYS) + 1)
            self.assertEqual(jitter.call_args_list, [call(10), call(30)])
            self.assertEqual(sleep.call_args_list, [call(7.5), call(24.0)])
            warning.assert_any_call("Retrying LLM request (attempt 2) after 7.5s...")
            warning.assert_any_call("Retrying LLM request (attempt 3) after 24.0s...")

    def test_nonretryable_errors_and_request_timeouts_do_not_sleep(self):
        for error in (ValueError("invalid response"), llm_utils.Timeout("request timeout")):
            with self.subTest(error=error), \
                 patch.object(llm_utils, "_is_openai_cloud", True), \
                 patch.object(llm_utils, "_call_responses_api", side_effect=error) as request, \
                 patch.object(llm_utils, "jitter") as jitter, \
                 patch.object(llm_utils.time, "sleep") as sleep:
                self.assertIsNone(llm_utils._call_llm(None, "image", "requirements", "overview"))
            request.assert_called_once()
            jitter.assert_not_called()
            sleep.assert_not_called()

    def test_interrupt_propagates_without_retrying(self):
        with patch.object(llm_utils, "_is_openai_cloud", True), \
             patch.object(llm_utils, "_call_responses_api", side_effect=KeyboardInterrupt) as request, \
             patch.object(llm_utils, "jitter") as jitter, \
             patch.object(llm_utils.time, "sleep") as sleep, \
             self.assertRaises(KeyboardInterrupt):
            llm_utils._call_llm(None, "image", "requirements", "overview")
        request.assert_called_once()
        jitter.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
