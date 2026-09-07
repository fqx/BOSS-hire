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


if __name__ == "__main__":
    unittest.main()
