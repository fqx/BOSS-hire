"""Native screening boundaries and local accessibility navigation."""
import asyncio
from copy import deepcopy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from native_driver import AXNode, NativeDriverError
import job_utils
import native_workflow


def node(path, description="", role="AXStaticText", actions=(), *, value="", y=100):
    return AXNode(
        path=tuple(path), role=role, title="", description=description,
        value=value, actions=tuple(actions),
        rect={"x": 100, "y": y, "width": 200, "height": 30},
    )


class NativeResumeParsingTests(unittest.TestCase):
    def test_parses_yuan_salary_and_native_fields(self):
        parsed = native_workflow.parse_native_resume(
            "黄先生\n32岁\n大专\n离职-随时到岗\n求职期望\n销售专员，广州\n5000-7000元\n工作经历\n医药销售三年"
        )
        self.assertEqual(parsed["age"], 32)
        self.assertEqual(parsed["education"], 4)
        self.assertEqual(parsed["job_status"], "离职-随时到岗")
        self.assertEqual(parsed["salary_lower_bound"], 5000)
        self.assertEqual(parsed["salary_upper_bound"], 7000)

    def test_salary_formats(self):
        for salary, lower, upper in [
            ("5,000-7,000元", 5000, 7000),
            ("0.8-1.2万元", 8000, 12000),
            ("8-12K", 8000, 12000),
            ("面议", -1, -1),
        ]:
            with self.subTest(salary=salary):
                parsed = native_workflow.parse_native_resume(f"32岁 大专\n求职期望\n{salary}\n工作经历")
                self.assertEqual(parsed["salary_lower_bound"], lower)
                self.assertEqual(parsed["salary_upper_bound"], upper)

    def test_prefilter_checks_full_resume_keyword_tree(self):
        requirements = job_utils.get_job_requirements({
            "cv_required_keywords": {"医药": ["销售", "研发"]},
        })
        text = "32岁 大专\n求职期望\n面议\n工作经历\n医药销售"
        self.assertEqual(native_workflow.prefilter_resume(text, requirements), "passed")
        self.assertEqual(native_workflow.prefilter_resume(text.replace("销售", "仓库"), requirements), "keywords")

    def test_prefilter_explicit_boundaries(self):
        text = "32岁 大专\n离职-随时到岗\n求职期望\n5000-7000元\n工作经历\n医药销售"
        for override, expected in [
            ({"age_upper_bound": 30}, "age"),
            ({"education": 5}, "education"),
            ({"maximum_salary": 4000}, "salary"),
            ({"gender": "女"}, "gender"),
            ({"cv_required_keywords": ["软件"]}, "keywords"),
        ]:
            with self.subTest(override=override):
                requirements = job_utils.get_job_requirements(override)
                self.assertEqual(native_workflow.prefilter_resume(text, requirements), expected)
        self.assertEqual(native_workflow.prefilter_resume(
            text.replace("离职-随时到岗", "在职-月内到岗"),
            job_utils.get_job_requirements({"off_the_job": 1}),
        ), "employment_status")

    def test_historical_salary_cannot_supply_hidden_expectation(self):
        parsed = native_workflow.parse_native_resume(
            "32岁 大专\n求职期望\n销售专员，广州\n工作经历\n过去薪资8-12K"
        )
        self.assertIsNone(parsed["salary_lower_bound"])

    def test_missing_expectation_does_not_extract_salary_from_prose(self):
        parsed = native_workflow.parse_native_resume("32岁 大专\n个人优势：期望业绩达到5000-7000元\n工作经历")
        self.assertIsNone(parsed["salary_lower_bound"])
        self.assertIsNone(parsed["salary_upper_bound"])

    def test_historical_age_and_degree_do_not_supply_profile_fields(self):
        parsed = native_workflow.parse_native_resume(
            "李先生\n求职期望\n销售，广州\n面议\n工作经历\n为本科院校提供服务，客户年龄在32岁左右"
        )
        self.assertIsNone(parsed["age"])
        self.assertIsNone(parsed["education"])

    def test_recently_followed_positions_do_not_supply_expected_salary_or_profile_fields(self):
        parsed = native_workflow.parse_native_resume(
            "赖女士\n32岁 大专\n在职-考虑机会\n最近关注\n"
            "牛人最近7天沟通过的职位,8000-12000元\n工作经历\n医药代表"
        )
        self.assertEqual(parsed["age"], 32)
        self.assertEqual(parsed["education"], 4)
        self.assertIsNone(parsed["salary_lower_bound"])
        self.assertIsNone(parsed["salary_upper_bound"])
        missing_profile = native_workflow.parse_native_resume(
            "赖女士\n最近关注\n本科，32岁，8000-12000元\n工作经历\n医药代表"
        )
        self.assertIsNone(missing_profile["age"])
        self.assertIsNone(missing_profile["education"])
        self.assertIsNone(missing_profile["salary_lower_bound"])

    def test_profile_prose_mentions_do_not_become_section_boundaries(self):
        for mention in ("最近关注医疗器械市场", "求职期望比较明确"):
            with self.subTest(mention=mention):
                parsed = native_workflow.parse_native_resume(
                    f"个人优势：{mention}。\n32岁 大专\n求职期望\n面议\n工作经历\n医药代表"
                )
                self.assertEqual(parsed["age"], 32)
                self.assertEqual(parsed["education"], 4)
                self.assertEqual(parsed["salary_lower_bound"], -1)


class NativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_recently_followed_resume_layout_is_valid_at_stopped_top_boundary(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollUpByPage",))
        nodes = [scroll, node((1,), "赖女士"), node((2,), "在职-考虑机会, 大专, 32岁"),
                 node((3,), "最近关注"), node((4,), "牛人最近7天沟通过的职位,8000-12000元"),
                 node((5,), "工作经历"), node((6,), "医药代表"),
                 node((7,), "立即沟通", "AXButton", ("AXPress",))]
        session = native_workflow.NativeSession(SimpleNamespace(perform=AsyncMock()))
        session.wait = AsyncMock(return_value=nodes)
        text = await session.read_resume()
        self.assertIn("赖女士", text)
        self.assertIn("最近关注", text)
        self.assertIn("工作经历", text)
        self.assertIn("医药代表", text)
        self.assertEqual(session.driver.perform.await_args_list,
                         [call(scroll, "scrollUp"), call(scroll, "scrollUp")])
        self.assertTrue(all(item.args[0](nodes) for item in session.wait.await_args_list))

    def test_virtualized_resume_remains_recognizable_without_top_headings(self):
        bottom = [node((0,), "继续沟通", "AXButton", ("AXPress",)),
                  node((1,), "不合适", "AXButton", ("AXPress",)),
                  node((2,), "井冈山大学")]
        self.assertTrue(native_workflow.is_resume(bottom))
        self.assertFalse(native_workflow.is_resume(bottom + [node((3,), "换电话/微信")]))
        self.assertFalse(native_workflow.is_resume(bottom + [node((3,), "牛人")]))

    async def test_long_virtualized_resume_expands_merges_cells_and_returns_to_top(self):
        def cell(index, employer, date, *, y=200):
            path = (0, index)
            return [node(path, role="AXGroup", y=y),
                    node(path + (0,), date, y=y),
                    node(path + (1,), "负责销售", y=y + 20),
                    node(path + (2,), employer, y=y + 40),
                    node(path + (3,), "销售专员", y=y + 60)]

        down = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        up = node((0,), role="AXGroup", actions=("AXScrollUpByPage",))
        contact = node((9,), "继续沟通", "AXButton", ("AXPress",))
        headings = [node((1,), "求职期望"), node((2,), "工作经历")]
        first_job = cell(0, "甲公司", "2024.06-2025.06")
        second_job = cell(1, "乙公司", "2022.01-2024.05", y=400)
        expand = node((3,), "展开全部工作经历", actions=("AXPress",))
        top_collapsed = [down] + headings + first_job + [expand, contact]
        top_expanded = [down] + headings + first_job + second_job + [contact]
        # UIKit reuses cell path (0, 0) for the second employer at the bottom.
        # Top headings disappear entirely and vertical down action disappears.
        bottom = [up] + cell(0, "乙公司", "2022.01-2024.05", y=-100) + [
            node((0, 1), role="AXGroup"), node((0, 1, 0), "教育经历"),
            node((0, 1, 1), "井冈山大学"), contact,
        ]
        driver = SimpleNamespace(perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[bottom, top_collapsed, top_expanded, bottom, top_expanded])
        text = await session.read_resume()
        for expected in ("甲公司", "乙公司", "井冈山大学"):
            self.assertIn(expected, text)
        self.assertEqual(text.count("乙公司"), 1)
        self.assertEqual(text.count("销售专员"), 2)
        self.assertEqual(text.count("负责销售"), 2)
        self.assertEqual([call.args for call in driver.perform.await_args_list],
                         [(up, "scrollUp"), (expand, "press"), (down, "scrollDown"), (up, "scrollUp")])
        for call, page in zip(session.wait.await_args_list, [bottom, top_collapsed, top_expanded, bottom, top_expanded]):
            self.assertTrue(call.args[0](page))

    async def test_expansion_failure_propagates_without_returning_partial_resume(self):
        expand = node((3,), "展开全部项目经历", actions=("AXPress",))
        nodes = [node((0,), "求职期望"), node((1,), "工作经历"), expand]
        driver = SimpleNamespace(perform=AsyncMock(side_effect=NativeDriverError("cannot expand")))
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(return_value=nodes)
        with self.assertRaisesRegex(NativeDriverError, "cannot expand"):
            await session.read_resume()

    async def test_top_boundary_is_confirmed_when_inherited_up_action_remains(self):
        inherited = node((0,), role="AXGroup", actions=("AXScrollUpByPage", "AXScrollDownByPage"))
        contact = node((9,), "继续沟通", "AXButton", ("AXPress",))
        top = [inherited, node((1,), "求职期望"), node((2,), "工作经历"),
               node((3,), "顶部工作内容"), contact]
        bottom_scroll = node((0,), role="AXGroup", actions=("AXScrollUpByPage",))
        bottom = [bottom_scroll, node((3,), "最后一段工作内容"), contact]
        driver = SimpleNamespace(perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[top, top, bottom, top, top])
        text = await session.read_resume()
        self.assertIn("顶部工作内容", text)
        self.assertIn("最后一段工作内容", text)
        self.assertEqual([call.args for call in driver.perform.await_args_list], [
            (inherited, "scrollUp"), (inherited, "scrollDown"),
            (bottom_scroll, "scrollUp"), (inherited, "scrollUp"),
        ])

    async def test_unchanged_up_scroll_without_top_metadata_fails(self):
        inherited = node((0,), role="AXGroup", actions=("AXScrollUpByPage",))
        incomplete = [inherited, node((1,), "工作经历的一部分"),
                      node((9,), "继续沟通", "AXButton", ("AXPress",))]
        driver = SimpleNamespace(perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[incomplete, incomplete])
        with self.assertRaisesRegex(NativeDriverError, "未识别到简历顶部资料"):
            await session.read_resume()
        driver.perform.assert_awaited_once_with(inherited, "scrollUp")

    async def test_selected_job_still_switches_to_recommend_feed(self):
        nodes = [node((0,), "牛人", "AXButton"),
                 node((1,), "推荐", "AXButton", ("AXPress",)),
                 node((2,), "市场专员", "AXButton", ("AXPress",), value="选中")]
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=nodes), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(return_value=nodes)
        session.apply_history_filter = AsyncMock(return_value=nodes)
        sequence = Mock()
        sequence.attach_mock(session.wait, "wait")
        sequence.attach_mock(driver.perform, "press")
        sequence.attach_mock(session.apply_history_filter, "history_filter")
        await session.select_job("市场专员")
        driver.perform.assert_awaited_once_with(nodes[1], "press")
        self.assertEqual(session.wait.await_count, 2)
        session.apply_history_filter.assert_awaited_once_with("市场专员")
        self.assertEqual([item[0] for item in sequence.mock_calls], ["wait", "press", "wait", "history_filter"])

    async def test_snapshot_propagates_transport_and_account_failures(self):
        for error in (ConnectionError("disconnected"), NativeDriverError("permission")):
            session = native_workflow.NativeSession(SimpleNamespace(snapshot=AsyncMock(side_effect=error)))
            with self.subTest(error=error), self.assertRaises(type(error)):
                await session.snapshot()
        session = native_workflow.NativeSession(SimpleNamespace(snapshot=AsyncMock(return_value=[node((0,), "人脸验证")])) )
        with self.assertRaisesRegex(NativeDriverError, "人工处理"):
            await session.snapshot()

    async def test_network_banner_still_stops_default_snapshot(self):
        banner = node((0,), "网络连接失败，请检查网络", "AXButton")
        session = native_workflow.NativeSession(SimpleNamespace(snapshot=AsyncMock(return_value=[banner])))
        with self.assertRaisesRegex(NativeDriverError, "网络连接失败"):
            await session.snapshot()

    async def test_navigation_exception_only_allows_network_error(self):
        banner = node((0,), "网络连接失败，请检查网络", "AXButton")
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=[banner]))
        session = native_workflow.NativeSession(driver)
        self.assertEqual(await session.snapshot(allow_network_error=True), [banner])
        driver.snapshot.return_value = [banner, node((1,), "人脸验证")]
        with self.assertRaisesRegex(NativeDriverError, "人工处理"):
            await session.snapshot(allow_network_error=True)

    async def test_select_job_requires_unique_exact_native_label(self):
        base = [node((0,), "牛人", "AXButton"), node((1,), "推荐", "AXButton")]
        for jobs in (
            [node((2,), "市场专员（广州）", "AXButton", ("AXPress",))],
            [node((2,), "市场专员", "AXButton", ("AXPress",)), node((3,), "市场专员", "AXButton", ("AXPress",))],
        ):
            driver = SimpleNamespace(snapshot=AsyncMock(return_value=base + jobs), perform=AsyncMock())
            with self.subTest(jobs=jobs), self.assertRaisesRegex(NativeDriverError, "唯一匹配"):
                await native_workflow.NativeSession(driver).select_job("市场专员")
            driver.perform.assert_not_awaited()

    async def test_select_job_leaves_offline_chat_then_strictly_checks_destination(self):
        chat = [node((0,), "网络连接失败，请检查网络", "AXButton"),
                node((1,), "求简历", "AXButton"), node((2,), "换电话/微信", "AXButton")]
        entry = node((3,), "牛人", "AXButton", ("AXPress",))
        navigation = [entry, node((4,), "消息", "AXButton")]
        job = node((5,), "市场专员", "AXButton", ("AXPress",))
        recommend = node((6,), "推荐", "AXButton", ("AXPress",))
        destination = [entry, job, recommend]
        selected = [entry, node((5,), "市场专员", "AXButton", ("AXPress",), value="选中"), recommend]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[chat, destination, selected]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.back = AsyncMock(return_value=navigation)
        session.wait = AsyncMock(side_effect=[destination, selected, selected])
        session.apply_history_filter = AsyncMock(return_value=selected)
        self.assertEqual(await session.select_job("市场专员"), selected)
        self.assertEqual(session.back.await_count, 1)
        self.assertEqual(session.back.await_args.kwargs, {"allow_network_error": True})
        self.assertTrue(session.back.await_args.args[0](navigation))
        self.assertEqual(session.wait.await_args_list[0].kwargs, {"allow_network_error": True})
        self.assertEqual([call.args for call in driver.perform.await_args_list],
                         [(entry, "press"), (job, "press"), (recommend, "press")])
        self.assertEqual(driver.snapshot.await_count, 3)
        session.apply_history_filter.assert_awaited_once_with("市场专员")

    async def test_select_job_network_error_at_destination_stops_before_job_press(self):
        banner = node((0,), "网络连接失败，请检查网络", "AXButton")
        chat = [banner, node((1,), "求简历", "AXButton"), node((2,), "换电话/微信", "AXButton")]
        entry = node((3,), "牛人", "AXButton", ("AXPress",))
        navigation = [entry, node((4,), "消息", "AXButton")]
        destination = [entry, node((5,), "推荐", "AXButton"),
                       node((6,), "市场专员", "AXButton", ("AXPress",)), banner]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[chat, destination]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.back = AsyncMock(return_value=navigation)
        session.wait = AsyncMock(return_value=destination)
        with self.assertRaisesRegex(NativeDriverError, "网络连接失败"):
            await session.select_job("市场专员")
        driver.perform.assert_awaited_once_with(entry, "press")

    async def test_read_resume_collects_text_across_scroll_and_confirms_bottom(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        first = [scroll, node((1,), "求职期望"), node((2,), "工作经历"), node((3,), "第一段工作", y=200)]
        second = [scroll, node((1,), "求职期望", y=-100), node((2,), "工作经历", y=-50),
                  node((4,), "教育经历", y=100), node((5,), "资格证书", y=150), node((6,), "最后一项证书", y=200)]
        driver = SimpleNamespace(perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[first, second, second])
        text = await session.read_resume()
        for expected in ("第一段工作", "教育经历", "资格证书", "最后一项证书"):
            self.assertIn(expected, text)
        self.assertEqual(text.count("工作经历"), 1)
        self.assertEqual(driver.perform.await_count, 2)
        self.assertTrue(all(call.args == (scroll, "scrollDown") for call in driver.perform.await_args_list))

    async def test_read_resume_scroll_failure_stops_instead_of_returning_partial_text(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        driver = SimpleNamespace(perform=AsyncMock(side_effect=NativeDriverError("stale")))
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(return_value=[scroll, node((1,), "求职期望"), node((2,), "工作经历")])
        with self.assertRaisesRegex(NativeDriverError, "stale"):
            await session.read_resume()

    async def test_read_resume_rejects_unconfirmed_end_after_page_limit(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        pages = [[scroll, node((1,), "求职期望"), node((2,), "工作经历", y=500-i)] for i in range(81)]
        driver = SimpleNamespace(perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=pages)
        with self.assertRaisesRegex(NativeDriverError, "未确认完整读取"):
            await session.read_resume()

    async def test_read_resume_preserves_same_title_at_different_nodes(self):
        nodes = [node((0,), "求职期望"), node((1,), "工作经历"),
                 node((2,), "销售专员"), node((3,), "甲公司"),
                 node((4,), "销售专员"), node((5,), "乙公司")]
        session = native_workflow.NativeSession(SimpleNamespace(perform=AsyncMock()))
        session.wait = AsyncMock(return_value=nodes)
        text = await session.read_resume()
        self.assertEqual(text.count("销售专员"), 2)
        self.assertLess(text.index("甲公司"), text.index("乙公司"))

    async def test_back_does_not_guess_if_multiple_navigation_buttons_match(self):
        window = node((), role="AXWindow")
        window.rect.update(x=0, y=0, width=788, height=1083)
        buttons = [node((i,), role="AXButton", actions=("AXPress",), y=50) for i in (1, 2)]
        for button in buttons:
            button.rect.update(x=10, width=25)
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=[window] + buttons), perform=AsyncMock())
        with self.assertRaisesRegex(NativeDriverError, "唯一定位"):
            await native_workflow.NativeSession(driver).back(lambda _: True, "list")
        driver.perform.assert_not_awaited()

    async def test_back_presses_unique_leading_arrow_and_confirms_list(self):
        window = node((), role="AXWindow")
        window.rect.update(x=0, y=0, width=788, height=1083)
        arrow = node((0,), role="AXButton", actions=("AXPress",), y=50)
        arrow.rect.update(x=10, width=25)
        chrome = node((1,), role="AXButton", actions=("AXPress",), y=5)
        chrome.rect.update(x=10, width=16, height=16)
        result = [node((2,), "牛人"), node((3,), "推荐")]
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=[window, arrow, chrome]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(return_value=result)
        self.assertEqual(await session.back(native_workflow.is_candidate_list, "候选人列表"), result)
        driver.perform.assert_awaited_once_with(arrow, "press")
        session.wait.assert_awaited_once_with(native_workflow.is_candidate_list, "候选人列表", timeout=5)

    @staticmethod
    def back_page(name="甲先生"):
        window = node((), role="AXWindow")
        window.rect.update(x=0, y=0, width=788, height=1083)
        arrow = node((0,), role="AXButton", actions=("AXPress",), y=50)
        arrow.rect.update(x=10, width=25)
        return [window, arrow, node((1,), name)]

    async def test_back_retries_once_when_original_page_is_unchanged(self):
        original = self.back_page()
        target = [node((2,), "牛人"), node((3,), "推荐")]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[original, original]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[TimeoutError("first press had no effect"), target])
        self.assertEqual(await session.back(native_workflow.is_candidate_list, "候选人列表"), target)
        self.assertEqual(driver.perform.await_count, 2)
        self.assertEqual([call.args for call in driver.perform.await_args_list],
                         [(original[1], "press"), (original[1], "press")])
        self.assertEqual([call.kwargs for call in session.wait.await_args_list], [{"timeout": 5}, {"timeout": 15}])

    async def test_back_does_not_retry_after_page_changed(self):
        original = self.back_page()
        changed = self.back_page("乙先生")
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[original, changed]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=TimeoutError("navigation unresolved"))
        with self.assertRaisesRegex(TimeoutError, "navigation unresolved"):
            await session.back(native_workflow.is_candidate_list, "候选人列表")
        driver.perform.assert_awaited_once_with(original[1], "press")
        self.assertEqual(session.wait.await_count, 1)

    async def test_back_waits_for_target_to_settle_without_pressing_again(self):
        original = self.back_page()
        target = [node((2,), "牛人"), node((3,), "推荐")]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[original, target]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[TimeoutError("target not stable yet"), target])
        self.assertEqual(await session.back(native_workflow.is_candidate_list, "候选人列表"), target)
        driver.perform.assert_awaited_once_with(original[1], "press")
        self.assertEqual([call.kwargs for call in session.wait.await_args_list], [{"timeout": 5}, {}])

    async def test_back_stops_if_second_press_also_has_no_effect(self):
        original = self.back_page()
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=[original, original, original]), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[TimeoutError("first"), TimeoutError("second")])
        with self.assertRaisesRegex(TimeoutError, "second"):
            await session.back(native_workflow.is_candidate_list, "候选人列表")
        self.assertEqual(driver.perform.await_count, 2)
        self.assertEqual(session.wait.await_count, 2)


class NativeHistoryFilterTests(unittest.IsolatedAsyncioTestCase):
    def recommendation(self, count=0):
        return [node((0,), "牛人", "AXButton"), node((1,), "推荐", "AXButton"),
                node((2,), "市场专员", "AXButton", value="选中"),
                node((3,), f"筛选·{count}" if count else "筛选", "AXButton", ("AXPress",))]

    def panel(self, count, row_index=2):
        return [node((10, 0), f"筛选·{count}" if count else "筛选"),
                node((10, 1), "近期没有看过"),
                node((10, row_index), "不限, 近14天没有", actions=("AXPress",)),
                node((10, 20), "不限, 近3天没有", actions=("AXPress",)),
                node((10, 21), "筛选说明：不改变其他条件", actions=("AXPress",)),
                node((10, 22), "重置", "AXButton", ("AXPress",)),
                node((10, 23), "确认", "AXButton", ("AXPress",))]

    async def test_saved_job_filters_load_before_history_toggle_uses_loaded_count(self):
        for initial_count, saved_count, history_enabled in ((0, 3, False), (5, 2, True), (4, 0, False)):
            with self.subTest(initial=initial_count, saved=saved_count, history_enabled=history_enabled):
                recommendation = self.recommendation(initial_count)
                application = node((10, 25), "应用", "AXButton", ("AXPress",))
                initial = self.panel(initial_count) + [node((10, 24), "是否应用该职位上次的筛选条件？"), application]
                loaded = self.panel(saved_count, row_index=5)
                changed = self.panel(saved_count - 1 if history_enabled else saved_count + 1, row_index=6)
                restored = self.panel(saved_count, row_index=7)
                final = self.recommendation(saved_count if history_enabled else saved_count + 1)
                pages = iter([initial, loaded, changed] + ([restored] if history_enabled else []) + [final])
                async def wait(predicate, description, **kwargs):
                    page = next(pages)
                    self.assertTrue(predicate(page), description)
                    return page
                driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
                session = native_workflow.NativeSession(driver)
                session.wait = AsyncMock(side_effect=wait)
                self.assertEqual(await session.apply_history_filter("市场专员"), final)
                expected = [(recommendation[-1], "press"), (application, "press"), (loaded[2], "press")]
                if history_enabled:
                    expected.append((changed[2], "press"))
                expected.append(((restored if history_enabled else changed)[-1], "press"))
                self.assertEqual([call.args for call in driver.perform.await_args_list], expected)
                loaded_predicate = session.wait.await_args_list[1].args[0]
                self.assertFalse(loaded_predicate(initial))
                self.assertFalse(loaded_predicate(final))

    async def test_no_saved_prompt_never_presses_unrelated_apply_button(self):
        recommendation = self.recommendation()
        application = node((10, 25), "应用", "AXButton", ("AXPress",))
        initial, changed = self.panel(0) + [application], self.panel(1) + [application]
        final = self.recommendation(1)
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[initial, changed, final])
        await session.apply_history_filter("市场专员")
        self.assertEqual([call.args for call in driver.perform.await_args_list],
                         [(recommendation[-1], "press"), (initial[2], "press"), (changed[-2], "press")])

    async def test_saved_prompt_missing_or_duplicate_apply_button_stops_before_other_filters(self):
        recommendation = self.recommendation()
        prompt = node((10, 24), "是否应用该职位上次的筛选条件？")
        application = node((10, 25), "应用", "AXButton", ("AXPress",))
        for applications in ([], [application, replace(application, path=(10, 26))],
                             [replace(application, role="AXStaticText")]):
            with self.subTest(applications=applications):
                driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
                session = native_workflow.NativeSession(driver)
                session.wait = AsyncMock(return_value=self.panel(0) + [prompt] + applications)
                with self.assertRaisesRegex(NativeDriverError, "应用按钮必须唯一"):
                    await session.apply_history_filter("市场专员")
                driver.perform.assert_awaited_once_with(recommendation[-1], "press")

    async def test_saved_filter_apply_error_or_persistent_prompt_never_toggles_history(self):
        recommendation = self.recommendation()
        application = node((10, 25), "应用", "AXButton", ("AXPress",))
        initial = self.panel(2) + [node((10, 24), "是否应用该职位上次的筛选条件？"), application]
        for apply_error in (None, NativeDriverError("saved filter application failed")):
            with self.subTest(apply_error=apply_error):
                driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation),
                                         perform=AsyncMock(side_effect=[None, apply_error]))
                session = native_workflow.NativeSession(driver)
                async def wait(predicate, description, **kwargs):
                    if predicate(initial):
                        return initial
                    raise TimeoutError("saved filter prompt remained")
                session.wait = AsyncMock(side_effect=wait)
                with self.assertRaises(type(apply_error) if apply_error else TimeoutError):
                    await session.apply_history_filter("市场专员")
                self.assertEqual([call.args for call in driver.perform.await_args_list],
                                 [(recommendation[-1], "press"), (application, "press")])

    async def test_history_toggle_off_or_on_preserves_other_filter_counts(self):
        for initial_count, was_enabled in ((0, False), (3, False), (1, True), (4, True)):
            with self.subTest(initial_count=initial_count, was_enabled=was_enabled):
                recommendation = self.recommendation(initial_count)
                initial = self.panel(initial_count)
                changed = self.panel(initial_count - 1 if was_enabled else initial_count + 1, row_index=4)
                restored = self.panel(initial_count, row_index=6)
                panels = [initial, changed] + ([restored] if was_enabled else [])
                final = self.recommendation(initial_count if was_enabled else initial_count + 1)
                pages = iter(panels + [final])
                async def wait(predicate, description, **kwargs):
                    page = next(pages)
                    self.assertTrue(predicate(page), description)
                    return page
                driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
                session = native_workflow.NativeSession(driver)
                session.wait = AsyncMock(side_effect=wait)
                result = await session.apply_history_filter("市场专员")
                self.assertEqual(result, final)
                expected = [(recommendation[-1], "press"), (initial[2], "press")]
                if was_enabled:
                    expected.append((changed[2], "press"))
                expected.append(((restored if was_enabled else changed)[-1], "press"))
                self.assertEqual([item.args for item in driver.perform.await_args_list], expected)
                # Confirming the panel must also verify the original selected job.
                wrong_job = [replace(n, value="未选中") if n.description == "市场专员" else n for n in final]
                self.assertFalse(session.wait.await_args_list[-1].args[0](wrong_job))
                self.assertFalse(session.wait.await_args_list[-1].args[0](final + panels[-1]))

    async def test_unchanged_count_times_out_without_confirming_or_pressing_again(self):
        recommendation = self.recommendation()
        panel = self.panel(0)
        async def wait(predicate, description, **kwargs):
            if predicate(panel):
                return panel
            raise TimeoutError("filter count unchanged")
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=wait)
        with self.assertRaisesRegex(TimeoutError, "count unchanged"):
            await session.apply_history_filter("市场专员")
        self.assertEqual([item.args for item in driver.perform.await_args_list],
                         [(recommendation[-1], "press"), (panel[2], "press")])

    async def test_unexpected_count_delta_stops_without_confirming(self):
        recommendation = self.recommendation(3)
        initial, unexpected = self.panel(3), self.panel(5)
        driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
        session = native_workflow.NativeSession(driver)
        session.wait = AsyncMock(side_effect=[initial, unexpected])
        with self.assertRaisesRegex(NativeDriverError, "计数"):
            await session.apply_history_filter("市场专员")
        self.assertEqual(driver.perform.await_count, 2)

    async def test_missing_or_ambiguous_exact_row_never_presses_other_filters(self):
        recommendation = self.recommendation()
        panel = self.panel(0)
        for rows in (panel[:2] + panel[3:], panel + [replace(panel[2], path=(10, 24))]):
            with self.subTest(rows=rows):
                driver = SimpleNamespace(snapshot=AsyncMock(return_value=recommendation), perform=AsyncMock())
                session = native_workflow.NativeSession(driver)
                session.wait = AsyncMock(return_value=rows)
                with self.assertRaisesRegex(NativeDriverError, "唯一"):
                    await session.apply_history_filter("市场专员")
                driver.perform.assert_awaited_once_with(recommendation[-1], "press")


class NativeRecommendationTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, resume=None, error=None):
        self.card = node((1,), "测试，10年以上，大专", actions=("AXPress",))
        return SimpleNamespace(
            wait=AsyncMock(return_value=[]),
            snapshot=AsyncMock(return_value=[node((0,), "求职期望"), node((1,), "工作经历"), node((2,), "测试候选人")]),
            candidate_cards=Mock(return_value=[(self.card, self.card.description)]),
            driver=SimpleNamespace(perform=AsyncMock()),
            read_resume=AsyncMock(return_value=resume, side_effect=error),
            return_to_candidates=AsyncMock(), press_label=AsyncMock(),
        )

    async def test_card_keyword_failure_skips_detail_and_model_with_card_diagnostic(self):
        session = self.make_session(resume="不应读取的详细简历")
        card_text = "测试，10年以上，大专\n医药仓储"
        session.candidate_cards.return_value = [(self.card, card_text)]
        requirements = job_utils.get_job_requirements({"cv_required_keywords": {"医药": ["销售", "研发"]}})
        stats = {}
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result") as assess, \
             patch.object(job_utils, "save_resume_diagnostic") as diagnostic:
            self.assertEqual(await native_workflow.loop_recommend(
                session, 1, requirements, None, stats, "市场专员",
            ), (0, 0))
        session.driver.perform.assert_not_awaited()
        session.read_resume.assert_not_awaited()
        session.return_to_candidates.assert_not_awaited()
        assess.assert_not_called()
        self.assertEqual(stats["市场专员"]["scanned"], 1)
        self.assertEqual(stats["市场专员"]["card_prefilter_skipped"], 1)
        diagnostic.assert_called_once_with(
            "市场专员", 1, job_utils.parse_resume(card_text), ["医药"], False, "card_keywords", card_text,
        )

    async def test_failed_card_does_not_stop_next_matching_candidate(self):
        session = self.make_session(resume="32岁 大专\n求职期望\n面议\n工作经历\n医药销售")
        matching = node((2,), "另一位，8年，大专，医药销售", actions=("AXPress",))
        session.candidate_cards.return_value = [(self.card, "测试，10年，大专，物流仓储"),
                                               (matching, matching.description)]
        requirements = job_utils.get_job_requirements({
            "cv_required_keywords": {"医药": ["销售", "研发"]}, "cv_requirements": "医药销售",
        })
        stats = {}
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)) as assess, \
             patch.object(job_utils, "save_resume_diagnostic"):
            self.assertEqual(await native_workflow.loop_recommend(
                session, 2, requirements, "client", stats, "市场专员",
            ), (1, 0))
        session.driver.perform.assert_awaited_once_with(matching, "press")
        session.read_resume.assert_awaited_once()
        session.return_to_candidates.assert_awaited_once()
        assess.assert_called_once()
        self.assertEqual(stats["市场专员"]["scanned"], 2)
        self.assertEqual(stats["市场专员"]["card_prefilter_skipped"], 1)

    async def test_card_keywords_reuse_nested_tree_and_empty_rules(self):
        cases = [
            ([], "物流仓储", True),
            ({"医药": {"销售": ["OTC", "处方药"]}}, "医药销售OTC", True),
            ({"医药": {"销售": ["OTC", "处方药"]}}, "医药销售", False),
            ({"医药": {"销售": ["OTC", "处方药"]}}, "软件销售OTC", False),
            ({"医药": {"销售": ["OTC", "处方药"]}, "医疗器械": ["售后"]}, "医疗器械售后", True),
        ]
        for tree, card_text, opens in cases:
            with self.subTest(tree=tree, card_text=card_text):
                session = self.make_session(resume="年龄未提供\n求职期望\n面议\n工作经历")
                session.candidate_cards.return_value = [(self.card, card_text)]
                stats = {}
                with patch.object(job_utils, "save_resume_diagnostic"), \
                     patch.object(native_workflow.llm_utils, "is_qualified_text_result") as assess:
                    self.assertEqual(await native_workflow.loop_recommend(
                        session, 1, job_utils.get_job_requirements({"cv_required_keywords": tree}),
                        None, stats, "市场专员",
                    ), (1 if opens else 0, 0))
                self.assertEqual(session.driver.perform.await_count, int(opens))
                self.assertEqual(session.read_resume.await_count, int(opens))
                self.assertEqual(session.return_to_candidates.await_count, int(opens))
                self.assertEqual(stats["市场专员"]["card_prefilter_skipped"], 0 if opens else 1)
                assess.assert_not_called()

    async def test_qualified_resume_default_does_not_contact_and_returns_to_list(self):
        resume = "32岁 大专\n求职期望\n面议\n工作经历\n医药销售"
        session = self.make_session(resume=resume)
        stats = {}
        result = SimpleNamespace(is_qualified=True)
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result", return_value=result) as assess, \
             patch.object(job_utils, "save_resume_diagnostic"):
            viewed, greeted = await native_workflow.loop_recommend(
                session, 1, job_utils.get_job_requirements({"cv_requirements": "医药销售"}),
                "client", stats, "市场专员",
            )
        self.assertEqual((viewed, greeted), (1, 0))
        self.assertEqual(stats["市场专员"]["qualified"], 1)
        assess.assert_called_once_with("client", resume, "医药销售")
        session.press_label.assert_not_awaited()
        session.return_to_candidates.assert_awaited_once()
        session.driver.perform.assert_awaited_once_with(self.card, "press")

    async def test_list_refresh_before_press_re_resolves_without_consuming_candidate(self):
        from native_driver import NativeStaleElementError
        session = self.make_session(resume="年龄未提供\n工作经历")
        fresh = replace(self.card, path=(9,))
        session.candidate_cards.side_effect = [[(self.card, self.card.description)], [(fresh, self.card.description)]]
        session.driver.perform.side_effect = [NativeStaleElementError("Stale accessibility path"), None]
        stats = {}
        with patch.object(job_utils, "save_resume_diagnostic"):
            self.assertEqual(await native_workflow.loop_recommend(
                session, 1, job_utils.get_job_requirements({}), None, stats, "市场专员"), (1, 0))
        self.assertEqual(stats["市场专员"]["scanned"], 1)
        self.assertEqual([c.args for c in session.driver.perform.await_args_list],
                         [(self.card, "press"), (fresh, "press")])
        session.read_resume.assert_awaited_once()

    async def test_repeated_stale_opens_stop_without_processing_or_contacting(self):
        from native_driver import NativeStaleElementError
        session = self.make_session(resume="unused")
        session.driver.perform.side_effect = NativeStaleElementError("Stale accessibility path")
        stats = {}
        with self.assertRaises(NativeStaleElementError):
            await native_workflow.loop_recommend(session, 1, job_utils.get_job_requirements({}), None, stats, "市场专员")
        self.assertEqual(session.driver.perform.await_count, 3)
        self.assertEqual(stats["市场专员"]["scanned"], 0)
        session.read_resume.assert_not_awaited()
        session.press_label.assert_not_awaited()

    async def test_other_open_failure_is_not_retried(self):
        session = self.make_session(resume="unused")
        session.driver.perform.side_effect = NativeDriverError("account unavailable")
        with self.assertRaisesRegex(NativeDriverError, "account unavailable"):
            await native_workflow.loop_recommend(session, 1, job_utils.get_job_requirements({}), None, {}, "市场专员")
        session.driver.perform.assert_awaited_once()
        session.read_resume.assert_not_awaited()

    async def test_resume_timeout_must_confirm_return_before_skipping(self):
        session = self.make_session(error=TimeoutError("loading"))
        result = await native_workflow.loop_recommend(
            session, 1, job_utils.get_job_requirements({}), None, {}, "市场专员",
        )
        self.assertEqual(result, (0, 0))
        session.return_to_candidates.assert_awaited_once()
        session.press_label.assert_not_awaited()

    async def test_failed_timeout_return_stops(self):
        session = self.make_session(error=TimeoutError("loading"))
        session.return_to_candidates.side_effect = NativeDriverError("cannot close")
        with self.assertRaisesRegex(NativeDriverError, "cannot close"):
            await native_workflow.loop_recommend(
                session, 2, job_utils.get_job_requirements({}), None, {}, "市场专员",
            )
        self.assertEqual(session.read_resume.await_count, 1)

    async def test_unexpected_resume_error_propagates(self):
        for error in (ConnectionError("closed"), NativeDriverError("account"), ValueError("unexpected")):
            session = self.make_session(error=error)
            with self.subTest(error=error), self.assertRaises(type(error)):
                await native_workflow.loop_recommend(
                    session, 2, job_utils.get_job_requirements({}), None, {}, "市场专员",
                )
            session.return_to_candidates.assert_not_awaited()
            self.assertEqual(session.read_resume.await_count, 1)

    async def test_overlapping_list_pages_are_deduplicated_and_bottom_ends_scan(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        card_a = node((0, 1, 0), "甲先生，10年以上，大专", actions=("AXPress",))
        employer_a = node((0, 1, 2), "甲公司·销售")
        card_b = node((0, 2, 0), "乙先生，8年，本科", actions=("AXPress",))
        employer_b = node((0, 2, 2), "乙公司·销售")
        page_one = [scroll, card_a, employer_a]
        page_two = [scroll, card_a, employer_a, card_b, employer_b]
        session = SimpleNamespace(
            wait=AsyncMock(side_effect=[page_one, page_one, page_two, page_two, page_two, page_two]),
            candidate_cards=native_workflow.NativeSession.candidate_cards,
            driver=SimpleNamespace(perform=AsyncMock()),
            read_resume=AsyncMock(return_value="年龄未提供\n求职期望\n面议\n工作经历"),
            return_to_candidates=AsyncMock(), press_label=AsyncMock(),
        )
        stats = {}
        with patch.object(job_utils, "save_resume_diagnostic"):
            self.assertEqual(await native_workflow.loop_recommend(
                session, 5, job_utils.get_job_requirements({}), None, stats, "市场专员",
            ), (2, 0))
        self.assertEqual(stats["市场专员"]["scanned"], 2)
        self.assertEqual(session.read_resume.await_count, 2)
        self.assertEqual(session.return_to_candidates.await_count, 2)
        self.assertEqual(
            [call.args for call in session.driver.perform.await_args_list],
            [(card_a, "press"), (scroll, "scrollDown"), (card_b, "press"), (scroll, "scrollDown")],
        )

    async def test_previously_viewed_cards_are_not_opened(self):
        session = self.make_session(resume="unused")
        session.candidate_cards.return_value = [(self.card, "已看过，测试，10年以上，大专")]
        stats = {}
        self.assertEqual(await native_workflow.loop_recommend(
            session, 1, job_utils.get_job_requirements({}), None, stats, "市场专员",
        ), (0, 0))
        self.assertEqual(stats["市场专员"]["scanned"], 1)
        session.driver.perform.assert_not_awaited()
        session.read_resume.assert_not_awaited()

    async def test_same_cards_moving_do_not_mistakenly_end_pagination(self):
        scroll = node((0,), role="AXGroup", actions=("AXScrollDownByPage",))
        card_a = node((0, 1, 0), "甲先生，10年以上，大专", actions=("AXPress",))
        card_a_moved = node((0, 1, 0), card_a.description, actions=("AXPress",), y=-100)
        card_b = node((0, 2, 0), "乙先生，8年，本科", actions=("AXPress",))
        first, moved, next_page = [scroll, card_a], [scroll, card_a_moved], [scroll, card_a_moved, card_b]
        session = SimpleNamespace(
            wait=AsyncMock(side_effect=[first, first, moved, moved, next_page, next_page]),
            candidate_cards=native_workflow.NativeSession.candidate_cards,
            driver=SimpleNamespace(perform=AsyncMock()),
            read_resume=AsyncMock(return_value="求职期望\n面议\n工作经历"),
            return_to_candidates=AsyncMock(), press_label=AsyncMock(),
        )
        stats = {}
        with patch.object(job_utils, "save_resume_diagnostic"):
            self.assertEqual(await native_workflow.loop_recommend(
                session, 2, job_utils.get_job_requirements({}), None, stats, "市场专员",
            ), (2, 0))
        self.assertEqual(stats["市场专员"]["scanned"], 2)
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(card_a, "press"), (scroll, "scrollDown"), (scroll, "scrollDown"), (card_b, "press")])

    async def test_contact_stops_if_candidate_changed_during_model_call(self):
        session = self.make_session(resume="32岁 大专\n求职期望\n面议\n工作经历\n医药销售")
        first = [node((0,), "求职期望"), node((1,), "工作经历"), node((2,), "甲先生")]
        changed = [node((0,), "求职期望"), node((1,), "工作经历"), node((2,), "乙先生")]
        session.snapshot.side_effect = [first, changed]
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch.object(job_utils, "save_resume_diagnostic"), \
             self.assertRaisesRegex(NativeDriverError, "候选人发生变化"):
            await native_workflow.loop_recommend(
                session, 1, job_utils.get_job_requirements({"cv_requirements": "医药销售"}),
                None, {}, "市场专员", allow_contact=True,
            )
        session.press_label.assert_not_awaited()
        session.return_to_candidates.assert_not_awaited()

    async def test_delivered_automatic_greeting_counts_once_without_sending_another_message(self):
        session = self.make_session(resume="32岁 大专\n求职期望\n面议\n工作经历\n医药销售")
        card = node((0, 8, 1, 0), "期望：销售，沟通的职位-市场专员", role="AXButton", actions=("AXPress",))
        chat = [card, node((0, 8, 2, 0), "求职者头像", role="AXButton"),
                node((0, 8, 2, 1), "送达", role="AXButton"),
                node((0, 8, 2, 2), "您好，方便聊聊我们的岗位吗？")]
        async def wait(predicate, description, **kwargs):
            if description == "候选人列表":
                return []
            if description == "沟通页面":
                return chat
            self.assertTrue(predicate(chat))
            return chat
        session.wait.side_effect = wait
        stats = {}
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch.object(job_utils, "save_resume_diagnostic"):
            self.assertEqual(await native_workflow.loop_recommend(
                session, 1, job_utils.get_job_requirements({"cv_requirements": "医药销售"}),
                None, stats, "市场专员", allow_contact=True,
            ), (1, 1))
        self.assertEqual(stats["市场专员"]["contact_opened"], 1)
        self.assertEqual(stats["市场专员"]["greeted"], 1)
        session.press_label.assert_awaited_once_with("立即沟通")
        session.return_to_candidates.assert_awaited_once()

    async def test_undelivered_initial_greeting_stops_without_resending_or_success_count(self):
        session = self.make_session(resume="32岁 大专\n求职期望\n面议\n工作经历\n医药销售")
        card = node((0, 8, 1, 0), "期望：销售，沟通的职位-市场专员", role="AXButton", actions=("AXPress",))
        chat = [card, node((0, 8, 2, 0), "求职者头像", role="AXButton"),
                node((0, 8, 2, 1), "发送中", role="AXButton"),
                node((0, 8, 2, 2), "您好，方便聊聊我们的岗位吗？")]
        async def wait(predicate, description, **kwargs):
            if description == "候选人列表":
                return []
            if description == "沟通页面":
                return chat
            self.assertFalse(predicate(chat))
            raise TimeoutError("automatic greeting not delivered")
        session.wait.side_effect = wait
        stats = {}
        with patch.object(native_workflow.llm_utils, "is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch.object(job_utils, "save_resume_diagnostic"), \
             self.assertRaisesRegex(TimeoutError, "not delivered"):
            await native_workflow.loop_recommend(
                session, 2, job_utils.get_job_requirements({"cv_requirements": "医药销售"}),
                None, stats, "市场专员", allow_contact=True,
            )
        self.assertEqual(stats["市场专员"]["contact_opened"], 1)
        self.assertEqual(stats["市场专员"]["greeted"], 0)
        session.read_resume.assert_awaited_once()
        session.driver.perform.assert_awaited_once_with(self.card, "press")
        session.press_label.assert_awaited_once_with("立即沟通")
        session.return_to_candidates.assert_not_awaited()


class NativeInitialGreetingTests(unittest.IsolatedAsyncioTestCase):
    def make_chat(self):
        return [
            node((0, 8, 1, 0), "候选人甲，沟通的职位-市场专员", role="AXButton", actions=("AXPress",)),
            node((0, 8, 2, 0), "求职者头像", role="AXButton"),
            node((0, 8, 2, 1), "送达", role="AXButton"),
            node((0, 8, 2, 2), "您好，方便聊聊我们的岗位吗？"),
        ]

    async def test_delivery_or_read_receipt_in_same_outgoing_history_cell_is_accepted(self):
        for status in ("送达", "已读"):
            with self.subTest(status=status):
                chat = self.make_chat()
                chat[2] = replace(chat[2], description=status)
                async def wait(predicate, description):
                    self.assertTrue(predicate(chat))
                    return chat
                session = SimpleNamespace(wait=AsyncMock(side_effect=wait),
                                          driver=SimpleNamespace(perform=AsyncMock()), press_label=AsyncMock())
                await native_workflow.confirm_initial_greeting(session, chat)
                session.driver.perform.assert_not_awaited()
                session.press_label.assert_not_awaited()

    async def test_delivery_requires_status_avatar_and_text_in_one_message_cell_in_history(self):
        chat = self.make_chat()
        invalid = {
            "outside history": [chat[0]] + [replace(n, path=(0, 9) + n.path[2:]) for n in chat[1:]],
            "status in another cell": [chat[0], chat[1], replace(chat[2], path=(0, 8, 3, 1)), chat[3]],
            "avatar in another cell": [chat[0], replace(chat[1], path=(0, 8, 3, 0)), chat[2], chat[3]],
            "text in another cell": chat[:3] + [replace(chat[3], path=(0, 8, 3, 2))],
            "incoming avatar": [chat[0], replace(chat[1], description="Boss头像"), chat[2], chat[3]],
            "empty text": chat[:3] + [replace(chat[3], description="")],
            "sending": [chat[0], chat[1], replace(chat[2], description="发送中"), chat[3]],
            "failed": [chat[0], chat[1], replace(chat[2], description="发送失败"), chat[3]],
        }
        for case, observed in invalid.items():
            with self.subTest(case=case):
                async def wait(predicate, description):
                    self.assertFalse(predicate(observed))
                    raise TimeoutError("not confirmed")
                session = SimpleNamespace(wait=AsyncMock(side_effect=wait),
                                          driver=SimpleNamespace(perform=AsyncMock()), press_label=AsyncMock())
                with self.assertRaises(TimeoutError):
                    await native_workflow.confirm_initial_greeting(session, chat)
                session.wait.assert_awaited_once()
                session.driver.perform.assert_not_awaited()
                session.press_label.assert_not_awaited()

    async def test_candidate_change_stops_even_when_another_chat_has_delivered_message(self):
        chat = self.make_chat()
        changed = [replace(chat[0], description="候选人乙，沟通的职位-市场专员")] + chat[1:]
        async def wait(predicate, description):
            return predicate(changed)
        session = SimpleNamespace(wait=AsyncMock(side_effect=wait),
                                  driver=SimpleNamespace(perform=AsyncMock()), press_label=AsyncMock())
        with self.assertRaisesRegex(NativeDriverError, "候选人发生变化"):
            await native_workflow.confirm_initial_greeting(session, chat)
        session.driver.perform.assert_not_awaited()
        session.press_label.assert_not_awaited()


class NativeRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_jobs_select_then_finish_screening_with_their_own_requirements(self):
        configs = [
            {"job_title": "市场专员", "max_idx": 1, "job_requirements": {
                "age_upper_bound": 35, "cv_required_keywords": {"医药": ["药品", "保健品"]},
                "cv_requirements": "市场岗位的完整要求",
            }},
            {"job_title": "销售专员", "max_idx": 2, "job_requirements": {
                "maximum_salary": 9000, "cv_requirements": "销售岗位的完整要求",
            }},
            {"job_title": "私域电商主播", "job_requirements": {
                "selector_job_title": "私域电商主播（广州）", "cv_requirements": "主播岗位的完整要求",
            }},
        ]
        original = deepcopy(configs)
        events = []
        async def select(title, selector):
            if events:
                self.assertEqual(events[-1][0], "screened")
            await asyncio.sleep(0)
            events.append(("selected", title))
        async def screen(session, maximum, requirements, client, stats, title, allow_contact):
            self.assertEqual(events[-1], ("selected", title))
            await asyncio.sleep(0)
            events.append(("screened", title))
            return 0, 0
        session = SimpleNamespace(select_job=AsyncMock(side_effect=select))
        stats = {}
        with patch.object(native_workflow, "NativeSession", return_value=session), \
             patch.object(native_workflow, "loop_recommend", new=AsyncMock(side_effect=screen)) as recommend, \
             patch("native_greetings.loop_greetings", new=AsyncMock()) as greetings:
            await native_workflow.run_native(configs, "client", stats, greetings=False, driver=object())
        self.assertEqual(events, [
            ("selected", "市场专员"), ("screened", "市场专员"),
            ("selected", "销售专员"), ("screened", "销售专员"),
            ("selected", "私域电商主播"), ("screened", "私域电商主播"),
        ])
        self.assertEqual(session.select_job.await_args_list, [
            call("市场专员", None), call("销售专员", None), call("私域电商主播", "私域电商主播（广州）"),
        ])
        self.assertEqual(recommend.await_args_list, [
            call(session, config.get("max_idx", 120), job_utils.get_job_requirements(config["job_requirements"]),
                 "client", stats, config["job_title"], False) for config in configs
        ])
        self.assertEqual(configs, original)
        greetings.assert_not_awaited()

    async def test_job_selection_or_screening_failure_stops_before_switching_to_next_job(self):
        configs = [{"job_title": "市场专员", "max_idx": 1, "job_requirements": {}},
                   {"job_title": "销售专员", "max_idx": 2, "job_requirements": {}}]
        for stage in ("select", "screen"):
            with self.subTest(stage=stage):
                failure = NativeDriverError(f"first job {stage} failed")
                session = SimpleNamespace(select_job=AsyncMock(side_effect=failure if stage == "select" else None))
                with patch.object(native_workflow, "NativeSession", return_value=session), \
                     patch.object(native_workflow, "loop_recommend", new=AsyncMock(side_effect=failure if stage == "screen" else None)) as recommend, \
                     patch("native_greetings.loop_greetings", new=AsyncMock()) as greetings, \
                     self.assertRaises(NativeDriverError) as raised:
                    await native_workflow.run_native(configs, "client", {}, greetings=False, driver=object())
                self.assertIs(raised.exception, failure)
                session.select_job.assert_awaited_once_with("市场专员", None)
                self.assertEqual(recommend.await_count, 1 if stage == "screen" else 0)
                greetings.assert_not_awaited()

    async def test_recommendation_only_routes_through_select_job_without_initial_health_snapshot(self):
        config = [{"job_title": "市场专员", "max_idx": 2, "job_requirements": {}}]
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=NativeDriverError("current chat offline")),
            select_job=AsyncMock(),
        )
        stats = {}
        driver = object()
        with patch.object(native_workflow, "NativeSession", return_value=session) as make_session, \
             patch.object(native_workflow, "loop_recommend", new=AsyncMock(return_value=(0, 0))) as recommend, \
             patch("native_greetings.loop_greetings", new=AsyncMock()) as greetings:
            await native_workflow.run_native(config, "client", stats, greetings=False, driver=driver)
        make_session.assert_called_once_with(driver)
        session.snapshot.assert_not_awaited()
        session.select_job.assert_awaited_once_with("市场专员", None)
        greetings.assert_not_awaited()
        recommend.assert_awaited_once_with(
            session, 2, job_utils.get_job_requirements({}), "client", stats, "市场专员", False,
        )


if __name__ == "__main__":
    unittest.main()
