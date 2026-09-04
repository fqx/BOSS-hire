import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_reason_can_revise_initial_rejection_before_qualified_marker(self):
        result = llm_utils.interviewer(
            reason=(
                "初步看候选人不符合，但复核后确认直播和舞蹈两项硬性要求均满足。"
                "\n最终结论：符合"
            ),
            reason_category="",
            is_qualified=True,
        )
        self.assertTrue(result.is_qualified)

    def test_rejects_boolean_that_conflicts_with_final_marker(self):
        with self.assertRaises(ValidationError):
            llm_utils.interviewer(
                reason="候选人缺少舞蹈基础。\n最终结论：不符合",
                reason_category="过往经历不符",
                is_qualified=True,
            )

    def test_parse_returns_failure_for_conflicting_structured_response(self):
        content = (
            '{"reason":"缺少舞蹈基础。\\n最终结论：不符合",'
            '"reason_category":"过往经历不符","is_qualified":true}'
        )
        with self.assertRaises(ValueError):
            llm_utils._parse_content(content)

    def test_public_apis_fail_closed_for_conflicting_structured_response(self):
        content = (
            '{"reason":"缺少舞蹈基础。\\n最终结论：不符合",'
            '"reason_category":"过往经历不符","is_qualified":true}'
        )
        client = self._client_returning(content)
        with patch.object(llm_utils, "_is_openai_cloud", False):
            self.assertFalse(
                llm_utils.is_qualified(client, "image", "requirements")
            )
            self.assertIsNone(
                llm_utils.is_qualified_result(client, "image", "requirements")
            )


if __name__ == "__main__":
    unittest.main()
