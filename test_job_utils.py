import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import commentjson
import driver_utils
import job_utils


class KeywordTreeTests(unittest.TestCase):
    def test_legacy_list_matches_any_keyword(self):
        for text, expected in (("A", True), ("B", True), ("ABC", True), ("X", False)):
            with self.subTest(text=text):
                self.assertEqual(job_utils.matches_keyword_tree(["A", "B", "C"], text), expected)
        self.assertTrue(job_utils.matches_keyword_tree([], "X"))

    def test_parent_and_one_child_are_both_required(self):
        tree = {"A": ["B", "C", "D"]}
        for text, expected in (
            ("AB", True), ("AC", True), ("AD", True), ("ABCD", True),
            ("A", False), ("BCD", False), ("X", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(job_utils.matches_keyword_tree(tree, text), expected)

    def test_nested_alternatives_require_a_complete_path(self):
        tree = {"A": {"B": ["C", "D"]}, "E": ["F"]}
        for text, expected in (
            ("ABC", True), ("ABD", True), ("EF", True),
            ("AB", False), ("AC", False), ("AE", False), ("BF", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(job_utils.matches_keyword_tree(tree, text), expected)

    def test_multiple_parent_branches_are_alternatives(self):
        tree = {"A": ["B", "C"], "D": ["E", "F"]}
        for text, expected in (
            ("AB", True), ("AC", True), ("DE", True), ("DF", True),
            ("AD", False), ("BE", False), ("AE", False), ("DB", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(job_utils.matches_keyword_tree(tree, text), expected)

    def test_list_can_mix_legacy_keywords_and_branches(self):
        tree = ["X", {"A": ["B", "C"]}]
        self.assertTrue(job_utils.matches_keyword_tree(tree, "X"))
        self.assertTrue(job_utils.matches_keyword_tree(tree, "AB"))
        self.assertFalse(job_utils.matches_keyword_tree(tree, "A"))

    def test_empty_children_only_require_parent(self):
        self.assertTrue(job_utils.matches_keyword_tree({"A": []}, "A"))
        self.assertFalse(job_utils.matches_keyword_tree({"A": []}, "B"))

    def test_diagnostics_include_matches_from_incomplete_branches(self):
        tree = {"A": ["B", "C"], "D": ["E"]}
        self.assertEqual(job_utils.get_matched_keywords(tree, "E C A"), ["A", "C", "E"])

    def test_requirements_accept_legacy_and_tree_configurations(self):
        for keywords in ([], ["A", "B"], {"A": ["B", "C"]}):
            with self.subTest(keywords=keywords):
                self.assertEqual(
                    job_utils.get_job_requirements({"cv_required_keywords": keywords})["cv_required_keywords"],
                    keywords,
                )

    def test_invalid_tree_is_rejected_when_loading_requirements(self):
        for keywords in (None, 1, [""], {"A": None}, {"A": [False]}, {"": ["B"]}):
            with self.subTest(keywords=keywords):
                with self.assertRaisesRegex(ValueError, "cv_required_keywords"):
                    job_utils.get_job_requirements({"cv_required_keywords": keywords})


class ResumeGenderTests(unittest.TestCase):
    def test_explicit_gender_preserves_existing_fields(self):
        text = "5-7K 测试候选人 28岁 大专 离职-随时到岗 期望：销售"
        original = job_utils.parse_resume(text)
        self.assertIsNone(original["gender"])
        for gender in ("男", "女"):
            with self.subTest(gender=gender):
                parsed = job_utils.parse_resume(text + "\n性别：" + gender)
                self.assertEqual(parsed, {**original, "gender": gender})

    def test_names_and_work_experience_do_not_imply_gender(self):
        for text in ("5-7K 张女士 28岁 女装销售", "5-7K 王先生 28岁 男装销售", "性别：未知", "优势：负责性别：女的客户"):
            with self.subTest(text=text):
                self.assertIsNone(job_utils.parse_resume(text)["gender"])


class ResumeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.resume = {
            "age": 30,
            "salary_lower_bound": 6000,
            "salary_upper_bound": 8000,
            "education": 4,
            "job_status": "离职-随时到岗",
        }

    def test_diagnostics_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "resume_cards.jsonl")
            with patch.dict(
                os.environ,
                {
                    "SAVE_RESUME_DIAGNOSTICS": "false",
                    "RESUME_DIAGNOSTICS_PATH": output_path,
                },
            ):
                job_utils.save_resume_diagnostic(
                    "私域电商主播", 1, self.resume, ["舞蹈"], True,
                    "passed", "30岁 舞蹈主播",
                )

            self.assertFalse(os.path.exists(output_path))

    def test_enabled_diagnostics_append_jsonl_without_image_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "diagnostics")
            output_path = os.path.join(output_dir, "resume_cards.jsonl")
            with patch.dict(
                os.environ,
                {
                    "SAVE_RESUME_DIAGNOSTICS": "true",
                    "RESUME_DIAGNOSTICS_PATH": output_path,
                },
            ):
                job_utils.save_resume_diagnostic(
                    "私域电商主播", 7, self.resume, ["舞蹈", "领舞"], True,
                    "passed", "30岁 舞蹈主播 领舞",
                )

            with open(output_path, encoding="utf-8") as input_file:
                record = json.loads(input_file.readline())

            self.assertEqual(record["job_title"], "私域电商主播")
            self.assertEqual(record["idx"], 7)
            self.assertEqual(record["matched_keywords"], ["舞蹈", "领舞"])
            self.assertTrue(record["prefilter_passed"])
            self.assertEqual(record["elimination_stage"], "passed")
            self.assertEqual(record["resume_card_text"], "30岁 舞蹈主播 领舞")
            self.assertNotIn("resume_image_base64", record)
            self.assertEqual(stat.S_IMODE(os.stat(output_path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(output_dir).st_mode), 0o700)

    def test_diagnostic_write_error_does_not_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "resume_cards.jsonl")
            with (
                patch.dict(
                    os.environ,
                    {
                        "SAVE_RESUME_DIAGNOSTICS": "true",
                        "RESUME_DIAGNOSTICS_PATH": output_path,
                    },
                ),
                patch.object(job_utils.os, "open", side_effect=PermissionError("denied")),
            ):
                saved = job_utils.save_resume_diagnostic(
                    "私域电商主播", 7, self.resume, ["舞蹈"], True,
                    "passed", "30岁 舞蹈主播",
                )

            self.assertFalse(saved)

    def test_existing_diagnostics_directory_is_tightened_before_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "diagnostics")
            os.mkdir(output_dir, mode=0o755)
            os.chmod(output_dir, 0o755)
            output_path = os.path.join(output_dir, "resume_cards.jsonl")
            with patch.dict(
                os.environ,
                {
                    "SAVE_RESUME_DIAGNOSTICS": "true",
                    "RESUME_DIAGNOSTICS_PATH": output_path,
                },
            ):
                saved = job_utils.save_resume_diagnostic(
                    "私域电商主播", 7, self.resume, ["舞蹈"], True,
                    "passed", "30岁 舞蹈主播",
                )

            self.assertTrue(saved)
            self.assertEqual(stat.S_IMODE(os.stat(output_dir).st_mode), 0o700)

    def test_diagnostics_chmod_error_refuses_write_without_escaping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "diagnostics")
            os.mkdir(output_dir)
            output_path = os.path.join(output_dir, "resume_cards.jsonl")
            with (
                patch.dict(
                    os.environ,
                    {
                        "SAVE_RESUME_DIAGNOSTICS": "true",
                        "RESUME_DIAGNOSTICS_PATH": output_path,
                    },
                ),
                patch.object(job_utils.os, "chmod", side_effect=PermissionError("denied")),
            ):
                saved = job_utils.save_resume_diagnostic(
                    "私域电商主播", 7, self.resume, ["舞蹈"], True,
                    "passed", "30岁 舞蹈主播",
                )

            self.assertFalse(saved)
            self.assertFalse(os.path.exists(output_path))

    def test_keyword_matches_preserve_configuration_order(self):
        matched = job_utils.get_matched_keywords(
            ["舞蹈", "团播", "主播", "领舞"],
            "有团播及领舞经验",
        )
        self.assertEqual(matched, ["团播", "领舞"])

    def test_sales_config_loads_with_commentjson(self):
        config_path = Path(__file__).with_name("销售专员.json")
        with config_path.open(encoding="utf-8") as config_file:
            configs = commentjson.load(config_file)
        self.assertEqual(
            [config["job_title"] for config in configs[:2]],
            ["私域电商主播", "社群运营专员"],
        )

    def test_active_sales_config_selectors_match_the_exact_live_job_labels(self):
        config_path = Path(__file__).with_name("销售专员.json")
        with config_path.open(encoding="utf-8") as config_file:
            configs = commentjson.load(config_file)
        option_labels = [
            "私域电商主播 _ 广州 9-12K",
            "私域电商主播 _ 广州 15-20K 待",
            "社群运营专员 _ 广州 4-7K",
        ]
        expected_labels = {
            "私域电商主播": "私域电商主播 _ 广州 9-12K",
            "社群运营专员": "社群运营专员 _ 广州 4-7K",
        }

        self.assertEqual(len(configs), 2)
        for config in configs:
            requirements = job_utils.get_job_requirements(config["job_requirements"])
            selected = driver_utils._select_job_option_label(
                option_labels,
                config["job_title"],
                requirements["selector_job_title"],
            )
            self.assertEqual(selected, expected_labels[config["job_title"]])

        social_requirements = job_utils.get_job_requirements(
            configs[1]["job_requirements"]
        )
        self.assertEqual(social_requirements["maximum_salary"], 6500)
        self.assertEqual(
            social_requirements["selector_job_title"],
            "社群运营专员 _ 广州 4-7K",
        )

    def test_legacy_requirements_keep_optional_selector_unset(self):
        requirements = job_utils.get_job_requirements({"maximum_salary": 6500})
        self.assertIsNone(requirements["selector_job_title"])
        self.assertEqual(
            driver_utils._select_job_option_label(
                ["社群运营专员 _ 广州 4-7K"],
                "社群运营专员",
                requirements["selector_job_title"],
            ),
            "社群运营专员 _ 广州 4-7K",
        )


class ResumeAgeDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_age_rejection_is_saved_when_card_is_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "resume_cards.jsonl")
            with (
                patch.dict(
                    os.environ,
                    {
                        "SAVE_RESUME_DIAGNOSTICS": "true",
                        "RESUME_DIAGNOSTICS_PATH": output_path,
                    },
                ),
                patch.object(
                    job_utils.driver_utils,
                    "get_resume_card_text",
                    new=AsyncMock(return_value="22岁 5-7K 大专 舞蹈教练"),
                ),
            ):
                await job_utils.save_age_rejection_diagnostic(
                    object(), "私域电商主播", 3, 22, ["舞蹈", "舞蹈教练"]
                )

            with open(output_path, encoding="utf-8") as input_file:
                record = json.loads(input_file.readline())
            self.assertEqual(record["elimination_stage"], "age")
            self.assertEqual(record["age"], 22)
            self.assertEqual(record["matched_keywords"], ["舞蹈", "舞蹈教练"])

    async def test_unreadable_age_card_is_saved_as_parse_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "resume_cards.jsonl")
            with (
                patch.dict(
                    os.environ,
                    {
                        "SAVE_RESUME_DIAGNOSTICS": "true",
                        "RESUME_DIAGNOSTICS_PATH": output_path,
                    },
                ),
                patch.object(
                    job_utils.driver_utils,
                    "get_resume_card_text",
                    new=AsyncMock(return_value=""),
                ),
            ):
                await job_utils.save_age_rejection_diagnostic(
                    object(), "私域电商主播", 4, 51, ["舞蹈"]
                )

            with open(output_path, encoding="utf-8") as input_file:
                record = json.loads(input_file.readline())
            self.assertEqual(record["elimination_stage"], "parse_error")
            self.assertEqual(record["age"], 51)


if __name__ == "__main__":
    unittest.main()
