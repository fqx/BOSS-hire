import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from native_driver import AXNode, NativeDriverError, NativeNetworkError
from native_greetings import (
    GREETING_QUALIFIED_MSG, SAVED_GREETING_MSG, chat_position, greeting_rows, loop_greetings,
    mark_unsuitable, match_greeting_job, open_detail_unsuitable_panel, platform_reason_label,
    send_qualified_message, send_saved_greeting,
)


def node(text, role="AXButton", value="", path=(0,)):
    return AXNode(path, role, "", text, value,
                  {"x": 0, "y": 0, "width": 100, "height": 30}, ("AXPress",))


LIST = [node("新招呼", "AXStaticText"), node("批量处理"), node("消息"), node("未读"),
        node("郑女士, 应聘主播, 传媒公司-主播, 大专 | 1.5-2万元", "AXStaticText", path=(1,))]
CARD = node("牛人分析器，期望：带货主播，10月30日 沟通的职位-私域电商主播", path=(0, 8, 1, 0))
CHAT = [node("求简历"), node("换电话/微信"), CARD]
EMPTY = [node("新招呼", "AXStaticText"), node("批量处理"), node("暂无新招呼")]
CONFIGS = [{"job_title": "私域电商主播", "job_requirements": {"cv_requirements": "主播经验"}}]
RESUME = [node("郑女士", "AXStaticText"), node("求职期望", "AXStaticText"),
          node("工作经历", "AXStaticText"), node("主播经验", "AXStaticText")]
REASON = node("过往经历不符", "AXStaticText", path=(5,))
REASON_PANEL = CHAT + [node("选择原因，为您优化推荐", "AXStaticText"), REASON,
                       node("礼貌通知对方", "AXStaticText")]
CONFIRM_BUTTON = node("确认", path=(6,))
REASON_CONFIRM = CHAT + [replace(REASON, value="选中"), CONFIRM_BUTTON]
MARKED = CHAT + [node("已标记不合适", "AXStaticText")]
REAL_PANEL = [
    node("选择原因，推荐更合适牛人", "AXStaticText", path=(0, 1, 0, 0)),
    node("牛人距离远", "AXStaticText", path=(0, 1, 0, 2, 0, 0)),
    node("不考虑 中专/中技", "AXStaticText", path=(0, 1, 0, 2, 1, 0)),
    node("期望薪资偏高", "AXStaticText", path=(0, 1, 0, 2, 2, 0)),
    node("年龄不合适", "AXStaticText", path=(0, 1, 0, 2, 3, 0)),
    node("工作经历和市场推广/地推无关", "AXStaticText", path=(0, 1, 0, 2, 4, 0)),
    node("活跃度低", "AXStaticText", path=(0, 1, 0, 2, 5, 0)),
    node("重复推荐", "AXStaticText", path=(0, 1, 0, 2, 6, 0)),
    node("其他原因", "AXStaticText", path=(0, 1, 0, 2, 7, 0)),
]
GENERATED_MESSAGE = "您好，看到您有直播带货和健康产品讲解经验，与我们的私域电商主播岗位方向相符，想了解您是否有兴趣进一步沟通，方便分享最新简历吗？"


class NativeGreetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_unread_entry_returns_without_opening_any_conversation(self):
        page = [n for n in LIST if n.description != "未读"]
        async def perform(control, action):
            self.assertEqual(control.description, "新招呼")
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=page),
            press_label=AsyncMock(), wait=AsyncMock(return_value=page),
            driver=SimpleNamespace(perform=AsyncMock(side_effect=perform)),
            read_resume=AsyncMock(),
        )
        with patch("native_greetings.llm_utils.is_qualified_text_result") as evaluate:
            self.assertEqual(await loop_greetings(session, CONFIGS, None, {}), 0)
        session.read_resume.assert_not_awaited()
        evaluate.assert_not_called()
        session.press_label.assert_not_awaited()
        session.driver.perform.assert_awaited_once_with(page[0], "press")

    async def test_mark_chooses_reason_then_confirms_once_and_checks_explicit_success(self):
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[CHAT, REASON_CONFIRM]),
            press_label=AsyncMock(), driver=SimpleNamespace(perform=AsyncMock()),
            wait=AsyncMock(side_effect=[REASON_PANEL, REASON_CONFIRM, MARKED]),
        )
        await mark_unsuitable(session, "过往经历不符", CARD.description)
        session.press_label.assert_awaited_once_with("不合适")
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(REASON, "press"), (CONFIRM_BUTTON, "press")])
        for call, page in zip(session.wait.await_args_list, [REASON_PANEL, REASON_CONFIRM, MARKED]):
            self.assertTrue(call.args[0](page))
        self.assertFalse(session.wait.await_args_list[-1].args[0](CHAT))

    async def test_mark_invalid_reason_or_changed_candidate_never_opens_panel(self):
        changed_chat = CHAT[:-1] + [replace(CARD, description="另一人，沟通的职位-私域电商主播")]
        for reason, current, message in [("自行编造的原因", CHAT, "无效"),
                                         ("过往经历不符", changed_chat, "候选人发生变化")]:
            with self.subTest(reason=reason):
                session = SimpleNamespace(snapshot=AsyncMock(return_value=current),
                                          press_label=AsyncMock(), driver=SimpleNamespace(perform=AsyncMock()))
                with self.assertRaisesRegex(NativeDriverError, message):
                    await mark_unsuitable(session, reason, CARD.description)
                session.press_label.assert_not_awaited()
                session.driver.perform.assert_not_awaited()

    async def test_mark_existing_reason_selection_is_preserved(self):
        session = SimpleNamespace(snapshot=AsyncMock(return_value=REASON_PANEL + [CONFIRM_BUTTON]),
                                  press_label=AsyncMock(), driver=SimpleNamespace(perform=AsyncMock()))
        with self.assertRaisesRegex(NativeDriverError, "已有原因选择"):
            await mark_unsuitable(session, "过往经历不符", CARD.description)
        session.driver.perform.assert_not_awaited()

    async def test_mark_duplicate_reason_is_not_guessed(self):
        session = SimpleNamespace(snapshot=AsyncMock(return_value=REASON_PANEL + [REASON]),
                                  driver=SimpleNamespace(perform=AsyncMock()))
        with self.assertRaisesRegex(NativeDriverError, "原因必须唯一"):
            await mark_unsuitable(session, "过往经历不符", CARD.description)
        session.driver.perform.assert_not_awaited()

    async def test_mark_wrong_reason_or_changed_chat_stops_before_confirmation(self):
        wrong_reason = CHAT + [node("学历不符", "AXStaticText", value="选中"), CONFIRM_BUTTON]
        changed_chat = REASON_CONFIRM.copy()
        changed_chat[2] = replace(CARD, description="另一人，沟通的职位-私域电商主播")
        for current, message in [(wrong_reason, "无法确认当前不合适原因"),
                                 (changed_chat, "候选人发生变化")]:
            with self.subTest(message=message):
                session = SimpleNamespace(
                    snapshot=AsyncMock(side_effect=[REASON_PANEL, current]),
                    driver=SimpleNamespace(perform=AsyncMock()),
                    wait=AsyncMock(return_value=REASON_CONFIRM),
                )
                with self.assertRaisesRegex(NativeDriverError, message):
                    await mark_unsuitable(session, "过往经历不符", CARD.description)
                session.driver.perform.assert_awaited_once_with(REASON, "press")

    async def test_other_reason_preset_does_not_hide_competing_selected_reason(self):
        from llm_utils import DISQUALIFICATION_REASON_CATEGORIES
        other = node("其他原因", "AXStaticText", path=(8,))
        all_reasons = [node(reason, "AXStaticText", path=(10, i))
                       for i, reason in enumerate(DISQUALIFICATION_REASON_CATEGORIES) if reason != "其他原因"] + [other]
        panel = CHAT + [node("选择原因，为您优化推荐", "AXStaticText")] + all_reasons
        wrong_confirm = CHAT + [replace(n, value="选中") if n.description == "学历不符" else n
                                for n in all_reasons] + [CONFIRM_BUTTON]
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[panel, wrong_confirm]),
            driver=SimpleNamespace(perform=AsyncMock()),
            wait=AsyncMock(return_value=wrong_confirm),
        )
        with self.assertRaisesRegex(NativeDriverError, "无法确认当前不合适原因"):
            await mark_unsuitable(session, "其他原因", CARD.description)
        session.driver.perform.assert_awaited_once_with(other, "press")
        # The readiness predicate itself must reject this state before submission.
        self.assertFalse(session.wait.await_args.args[0](wrong_confirm))

    async def test_mark_with_resume_binding_accepts_candidate_list_as_success(self):
        panel = RESUME + [node("选择原因，为您优化推荐", "AXStaticText"), REASON]
        confirm_page = RESUME + [replace(REASON, value="选中"), CONFIRM_BUTTON]
        candidate_list = [node("牛人", "AXStaticText"), node("推荐", "AXStaticText")]
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[RESUME, confirm_page]),
            press_label=AsyncMock(), driver=SimpleNamespace(perform=AsyncMock()),
            wait=AsyncMock(side_effect=[panel, confirm_page, candidate_list]),
        )
        await mark_unsuitable(
            session, "过往经历不符",
            verify_page=lambda ns: None,
            success_check=lambda ns: ns is candidate_list,
        )
        session.press_label.assert_awaited_once_with("不合适")
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(REASON, "press"), (CONFIRM_BUTTON, "press")])
        for call, page in zip(session.wait.await_args_list, [panel, confirm_page, candidate_list]):
            self.assertTrue(call.args[0](page))

    def test_platform_reason_mapping_uses_visible_presets(self):
        cases = [
            ("薪资不符", "期望薪资偏高"),
            ("学历不符", "不考虑 中专/中技"),
            ("年龄不符", "年龄不合适"),
            ("期望不符", "期望薪资偏高"),
            ("距离太远", "牛人距离远"),
            ("过往经历不符", "工作经历和市场推广/地推无关"),
            ("其他原因", "其他原因"),
            ("简历不真实", "其他原因"),
            ("已找到工作", "其他原因"),
        ]
        for category, expected in cases:
            with self.subTest(category=category):
                self.assertEqual(platform_reason_label(REAL_PANEL, category), expected)
        without_other = [n for n in REAL_PANEL if n.description != "其他原因"]
        with self.assertRaisesRegex(NativeDriverError, "未提供匹配"):
            platform_reason_label(without_other, "简历不真实")
        polluted = REAL_PANEL + [node("工作经历", "AXStaticText"), node("教育经历", "AXStaticText")]
        self.assertEqual(platform_reason_label(polluted, "过往经历不符"),
                         "工作经历和市场推广/地推无关")

    async def test_detail_menu_opens_unsuitable_from_rightmost_toolbar(self):
        window = AXNode((), "AXWindow", "", "BOSS直聘", "",
                        {"x": 21, "y": 445, "width": 788, "height": 1083}, ("AXRaise",))
        first = node("", path=(0, 1, 0, 1))
        second = node("", path=(0, 1, 0, 2))
        more = replace(node("", path=(0, 1, 0, 3)),
                       rect={"x": 769, "y": 492, "width": 31, "height": 34})
        menu_item = node("不合适", path=(0, 1, 2))
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=[window, first, second, more, node("求职期望")]),
            driver=SimpleNamespace(perform=AsyncMock()),
            wait=AsyncMock(return_value=[window, menu_item, node("举报")]),
        )
        await open_detail_unsuitable_panel(session)
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(more, "press"), (menu_item, "press")])

    async def test_mark_real_platform_reason_accepts_auto_submit_to_list(self):
        candidate_list = [node("牛人", "AXStaticText"), node("推荐", "AXStaticText")]
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=[node("求职期望"), node("工作经历")]),
            driver=SimpleNamespace(perform=AsyncMock()), press_label=AsyncMock(),
            wait=AsyncMock(side_effect=[REAL_PANEL, candidate_list]),
        )
        opener = AsyncMock()
        await mark_unsuitable(
            session, "过往经历不符",
            verify_page=lambda ns: None,
            success_check=lambda ns: ns is candidate_list,
            open_panel=opener,
            map_reason=lambda ns: platform_reason_label(ns, "过往经历不符"),
        )
        opener.assert_awaited_once_with(session)
        chosen = next(n for n in REAL_PANEL if n.description == "工作经历和市场推广/地推无关")
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(chosen, "press")])

    async def test_mark_with_advance_check_accepts_next_resume_after_reason(self):
        panel = REAL_PANEL
        initial = [node("求职期望", "AXStaticText"), node("工作经历", "AXStaticText")]
        next_resume = [node("求职期望", "AXStaticText"), node("工作经历", "AXStaticText")]
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=initial),
            driver=SimpleNamespace(perform=AsyncMock()), press_label=AsyncMock(),
            wait=AsyncMock(side_effect=[panel, next_resume]),
        )

        def verify(ns):
            if ns is panel or ns is initial:
                return
            raise NativeDriverError("候选人发生变化")

        await mark_unsuitable(
            session, "过往经历不符",
            verify_page=verify,
            success_check=lambda ns: False,
            open_panel=AsyncMock(),
            map_reason=lambda ns: platform_reason_label(ns, "过往经历不符"),
            advance_check=lambda ns: ns is next_resume,
        )
        chosen = next(n for n in REAL_PANEL if n.description == "工作经历和市场推广/地推无关")
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(chosen, "press")])

    async def test_mark_other_reason_enters_custom_text_and_confirms(self):
        initial = [node("求职期望", "AXStaticText", path=(0,)),
                   node("工作经历", "AXStaticText", path=(1,))]
        field = AXNode((0, 1, 0, 2), "AXTextArea", "", "", "",
                       {"x": 0, "y": 0, "width": 100, "height": 30}, ("AXPress",))
        dialog = [node("输入原因", "AXStaticText", path=(0, 1, 0, 0)),
                  node("请输入", "AXStaticText", path=(0, 1, 0, 1)),
                  field,
                  node("取消", path=(0, 1, 0, 6)),
                  node("确定", path=(0, 1, 0, 7))]
        filled = [replace(n, value="其他原因") if n is field else n for n in dialog]
        candidate_list = [node("牛人", "AXStaticText", path=(0,)),
                          node("推荐", "AXStaticText", path=(1,))]
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=initial),
            driver=SimpleNamespace(perform=AsyncMock(), set_value=AsyncMock()),
            press_label=AsyncMock(),
            wait=AsyncMock(side_effect=[REAL_PANEL, dialog, filled, candidate_list]),
        )
        await mark_unsuitable(
            session, "其他原因",
            verify_page=lambda ns: None,
            success_check=lambda ns: ns is candidate_list,
            open_panel=AsyncMock(),
        )
        session.driver.set_value.assert_awaited_once_with(field, "其他原因")
        reason_node = next(n for n in REAL_PANEL if n.description == "其他原因")
        confirm_node = next(n for n in filled if n.description == "确定")
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(reason_node, "press"), (confirm_node, "press")])

    async def test_mark_unconfirmed_submission_stops_without_retry(self):
        async def wait(predicate, description):
            if "原因选择" in description:
                self.assertTrue(predicate(REASON_CONFIRM))
                return REASON_CONFIRM
            self.assertFalse(predicate(CHAT))
            raise TimeoutError("mark not confirmed")
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[REASON_PANEL, REASON_CONFIRM]),
            driver=SimpleNamespace(perform=AsyncMock()), wait=AsyncMock(side_effect=wait),
        )
        with self.assertRaisesRegex(TimeoutError, "mark not confirmed"):
            await mark_unsuitable(session, "过往经历不符", CARD.description)
        self.assertEqual(session.driver.perform.await_count, 2)

    async def test_mark_success_on_another_candidate_cannot_confirm_this_submission(self):
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[REASON_PANEL, REASON_CONFIRM]),
            driver=SimpleNamespace(perform=AsyncMock()),
        )
        changed = MARKED.copy()
        changed[2] = replace(CARD, description="另一人，沟通的职位-私域电商主播")
        async def wait(predicate, description):
            page = REASON_CONFIRM if "原因选择" in description else changed
            self.assertTrue(predicate(page))
            return page
        session.wait = AsyncMock(side_effect=wait)
        with self.assertRaisesRegex(NativeDriverError, "候选人发生变化"):
            await mark_unsuitable(session, "过往经历不符", CARD.description)
        self.assertEqual(session.driver.perform.await_count, 2)

    async def test_enabled_unqualified_flow_counts_only_confirmed_mark(self):
        for error, landed_on_list in ((None, False), (None, True), (TimeoutError("not confirmed"), False)):
            with self.subTest(error=error, landed_on_list=landed_on_list):
                post_mark = EMPTY if landed_on_list else CHAT
                session = SimpleNamespace(
                    driver=SimpleNamespace(perform=AsyncMock()),
                    snapshot=AsyncMock(side_effect=[LIST, RESUME, RESUME, post_mark]),
                    press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
                    read_resume=AsyncMock(return_value="郑女士\n主播经验"),
                    back=AsyncMock(side_effect=[CHAT, EMPTY]),
                )
                stats = {}
                result = SimpleNamespace(is_qualified=False, reason_category="过往经历不符")
                with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=result), \
                     patch("native_greetings.mark_unsuitable", AsyncMock(side_effect=error)) as mark:
                    if error is None:
                        self.assertEqual(await loop_greetings(session, CONFIGS, None, stats, allow_contact=True), 1)
                    else:
                        with self.assertRaises(TimeoutError):
                            await loop_greetings(session, CONFIGS, None, stats, allow_contact=True)
                mark.assert_awaited_once()
                self.assertEqual(mark.await_args.args, (session, "过往经历不符"))
                self.assertEqual(set(mark.await_args.kwargs), {"verify_page", "success_check"})
                self.assertEqual(stats["私域电商主播"].get("marked_unsuitable", 0), 1 if error is None else 0)
                self.assertEqual(session.back.await_count, 1 if error is not None or landed_on_list else 2)

    async def test_unqualified_default_marks_and_review_decision_can_queue(self):
        for decision, marks in ((None, True), (True, True), (False, False), ("yes", False)):
            with self.subTest(decision=decision):
                reviewer = None if decision is None else AsyncMock(return_value=decision)
                post_mark = EMPTY if marks else None
                snapshot_pages = [LIST, RESUME, RESUME] + ([post_mark] if marks else [])
                session = SimpleNamespace(
                    driver=SimpleNamespace(perform=AsyncMock()),
                    snapshot=AsyncMock(side_effect=snapshot_pages),
                    press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
                    read_resume=AsyncMock(return_value="郑女士\n主播经验"),
                    back=AsyncMock(side_effect=[CHAT, EMPTY]),
                )
                result = SimpleNamespace(is_qualified=False, reason_category="过往经历不符", reason="完整评估理由")
                stats = {}
                with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=result), \
                     patch("native_greetings.mark_unsuitable", AsyncMock()) as mark:
                    self.assertEqual(await loop_greetings(session, CONFIGS, None, stats, allow_contact=True,
                                                          rejection_reviewer=reviewer), 1)
                if reviewer is not None:
                    reviewer.assert_awaited_once_with("私域电商主播", result)
                if marks:
                    mark.assert_awaited_once()
                    self.assertEqual(mark.await_args.args, (session, "过往经历不符"))
                    self.assertEqual(stats["私域电商主播"]["marked_unsuitable"], 1)
                    self.assertNotIn("awaiting_review", stats["私域电商主播"])
                else:
                    mark.assert_not_awaited()
                    self.assertEqual(stats["私域电商主播"]["awaiting_review"], 1)
                    self.assertEqual(stats["私域电商主播"]["pending_rejections"], [{
                        "reason": result.reason, "reason_category": "过往经历不符",
                        "expected_card_description": CARD.description,
                    }])

    async def test_reviewer_error_stops_before_mark_or_next_candidate(self):
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()),
            snapshot=AsyncMock(side_effect=[LIST, RESUME, RESUME]),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(return_value="郑女士\n主播经验"),
            back=AsyncMock(side_effect=[CHAT, EMPTY]),
        )
        result = SimpleNamespace(is_qualified=False, reason_category="过往经历不符")
        reviewer = AsyncMock(side_effect=RuntimeError("review interrupted"))
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=result), \
             patch("native_greetings.mark_unsuitable", AsyncMock()) as mark, \
             self.assertRaisesRegex(RuntimeError, "review interrupted"):
            await loop_greetings(session, CONFIGS, None, {}, allow_contact=True, rejection_reviewer=reviewer)
        mark.assert_not_awaited()
        session.back.assert_awaited_once()

    async def test_qualified_reply_does_not_require_rejection_review(self):
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()),
            snapshot=AsyncMock(side_effect=[LIST, RESUME, RESUME, CHAT]),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(return_value="郑女士\n主播经验"),
            back=AsyncMock(side_effect=[CHAT, EMPTY]),
        )
        reviewer = AsyncMock()
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True, greeting_message=GENERATED_MESSAGE)), \
             patch("native_greetings.send_saved_greeting", AsyncMock()) as send, \
             patch("native_greetings.send_qualified_message", AsyncMock()) as personalized:
            await loop_greetings(session, CONFIGS, None, {}, allow_contact=True, rejection_reviewer=reviewer)
        reviewer.assert_not_awaited()
        send.assert_awaited_once_with(session)
        personalized.assert_not_awaited()

    def test_position_match_unique_exact_or_annotated(self):
        self.assertIs(match_greeting_job("私域电商主播", CONFIGS), CONFIGS[0])
        self.assertIs(match_greeting_job("私域电商主播（广州）", CONFIGS), CONFIGS[0])
        self.assertIsNone(match_greeting_job("私域电商主播助理", CONFIGS))
        with self.assertRaisesRegex(NativeDriverError, "多个"):
            match_greeting_job("私域电商主播", CONFIGS + CONFIGS)

    def test_chat_position_requires_one_explicit_card(self):
        self.assertEqual(chat_position(CHAT)[0], "私域电商主播")
        with self.assertRaises(NativeDriverError):
            chat_position(CHAT + [CARD])
        self.assertEqual(greeting_rows(LIST), [LIST[-1]])

    async def run_read_only(self, result):
        driver = SimpleNamespace(perform=AsyncMock(), set_value=AsyncMock())
        session = SimpleNamespace(
            driver=driver, snapshot=AsyncMock(return_value=LIST),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(return_value="郑女士\n工作经历\n主播经验"),
            back=AsyncMock(side_effect=[CHAT, EMPTY]),
        )
        stats = {}
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=result):
            processed = await loop_greetings(session, CONFIGS, None, stats)
        self.assertEqual(processed, 1)
        self.assertEqual(driver.perform.await_count, 4)
        self.assertEqual([call.args[0].description for call in driver.perform.await_args_list[:2]], ["新招呼", "未读"])
        session.press_label.assert_not_awaited()
        driver.set_value.assert_not_awaited()
        self.assertEqual(session.back.await_count, 2)
        for call in session.back.await_args_list:
            self.assertTrue(call.args[0](CHAT if "关闭" in call.args[1] else EMPTY))
        return stats

    async def test_dry_run_qualified_never_sends(self):
        stats = await self.run_read_only(SimpleNamespace(is_qualified=True))
        self.assertEqual(stats["私域电商主播"]["inbound_qualified"], 1)
        self.assertNotIn("requested", stats["私域电商主播"])

    async def test_dry_run_unqualified_never_marks(self):
        await self.run_read_only(SimpleNamespace(is_qualified=False, reason_category="过往经历不符"))

    async def test_llm_failure_returns_to_list_without_action(self):
        stats = await self.run_read_only(None)
        self.assertEqual(stats["私域电商主播"]["inbound_viewed"], 1)

    async def test_unconfigured_chat_is_returned_without_reading(self):
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()), snapshot=AsyncMock(return_value=LIST),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(), back=AsyncMock(return_value=EMPTY),
        )
        self.assertEqual(await loop_greetings(session, [{"job_title": "销售专员"}], None, {}), 0)
        session.read_resume.assert_not_awaited()
        session.back.assert_awaited_once()

    async def test_resume_timeout_requires_verified_return(self):
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()), snapshot=AsyncMock(side_effect=[LIST, EMPTY]),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(side_effect=TimeoutError()),
            back=AsyncMock(side_effect=NativeDriverError("return failed")),
        )
        with self.assertRaisesRegex(NativeDriverError, "return failed"):
            await loop_greetings(session, CONFIGS, None, {})

    async def test_resume_network_banner_returns_to_list_without_processing(self):
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()), snapshot=AsyncMock(return_value=LIST),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(side_effect=NativeNetworkError("BOSS 网络连接失败")),
            back=AsyncMock(side_effect=[CHAT, EMPTY]),
        )
        self.assertEqual(await loop_greetings(session, CONFIGS, None, {}), 0)
        session.read_resume.assert_awaited_once()
        self.assertEqual(session.back.await_count, 2)
        for call in session.back.await_args_list:
            self.assertTrue(call.kwargs["allow_network_error"])

    async def test_send_does_not_overwrite_existing_draft(self):
        session = SimpleNamespace(snapshot=AsyncMock(return_value=CHAT + [node("", "AXTextArea", "草稿", path=(0, 9, 0))]),
                                  driver=SimpleNamespace(set_value=AsyncMock(), type_text=AsyncMock()))
        with self.assertRaisesRegex(NativeDriverError, "已有内容"):
            await send_qualified_message(session)
        session.driver.set_value.assert_not_awaited()
        session.driver.type_text.assert_not_awaited()

    async def test_send_attempt_once_then_confirmation_failure_stops(self):
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=CHAT + [node("", "AXTextArea", path=(0, 9, 0))]),
            driver=SimpleNamespace(set_value=AsyncMock()), press_label=AsyncMock(),
            wait=AsyncMock(side_effect=[[], TimeoutError("not confirmed")]),
        )
        with self.assertRaises(TimeoutError):
            await send_qualified_message(session)
        session.press_label.assert_awaited_once_with("发送")
        session.driver.set_value.assert_awaited_once()
        self.assertEqual(session.driver.set_value.await_args.args[1], GREETING_QUALIFIED_MSG)

    async def test_generated_message_is_written_once_and_confirmed_in_chat_history(self):
        field = node("", "AXTextArea", path=(0, 9, 0))
        written = CHAT + [replace(field, value=GENERATED_MESSAGE)]
        sent = CHAT + [field, node(GENERATED_MESSAGE, "AXStaticText", path=(0, 8, 2, 1))]
        async def wait(predicate, description):
            page = written if "写入" in description else sent
            self.assertTrue(predicate(page))
            return page
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[CHAT + [field], written]),
            driver=SimpleNamespace(set_value=AsyncMock(), type_text=AsyncMock()),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=wait),
        )
        await send_qualified_message(session, GENERATED_MESSAGE)
        session.driver.set_value.assert_awaited_once_with(field, GENERATED_MESSAGE)
        session.driver.type_text.assert_not_awaited()
        session.press_label.assert_awaited_once_with("发送")
        self.assertFalse(session.wait.await_args.args[0](written))

    async def test_generic_editor_uses_verified_typing_with_expected_chat_card(self):
        editor = node("和Ta聊一聊~", "AXGenericElement", path=(0, 9, 0))
        sent = CHAT + [editor, node(GENERATED_MESSAGE, "AXStaticText", path=(0, 8, 2, 1))]
        async def wait(predicate, description):
            self.assertTrue(predicate(sent))
            return sent
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=CHAT + [editor]),
            driver=SimpleNamespace(type_text=AsyncMock(return_value=GENERATED_MESSAGE), set_value=AsyncMock()),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=wait),
        )
        await send_qualified_message(session, GENERATED_MESSAGE)
        session.driver.type_text.assert_awaited_once_with(editor, GENERATED_MESSAGE, CARD.description)
        session.driver.set_value.assert_not_awaited()
        session.press_label.assert_awaited_once_with("发送")

    async def test_generic_editor_unverified_typing_stops_before_send(self):
        editor = node("和Ta聊一聊~", "AXGenericElement", path=(0, 9, 0))
        for returned in ("", "其他文本", False):
            with self.subTest(returned=returned):
                session = SimpleNamespace(
                    snapshot=AsyncMock(return_value=CHAT + [editor]),
                    driver=SimpleNamespace(type_text=AsyncMock(return_value=returned)),
                    press_label=AsyncMock(), wait=AsyncMock(),
                )
                with self.assertRaisesRegex(NativeDriverError, "输入内容未通过核对"):
                    await send_qualified_message(session, GENERATED_MESSAGE)
                session.press_label.assert_not_awaited()
                session.wait.assert_not_awaited()

    async def test_nonempty_uikit_composer_is_located_between_toolbar_controls(self):
        left = replace(node("常用语"), rect={"x": 10, "y": 500, "width": 20, "height": 20})
        right = replace(node("表情"), rect={"x": 300, "y": 500, "width": 20, "height": 20})
        editor = replace(node("已有草稿", "AXStaticText", path=(0, 9, 0)),
                         rect={"x": 40, "y": 498, "width": 250, "height": 25})
        # A matching history bubble must never be chosen as the composer.
        bubble = replace(editor, path=(0, 8, 3, 1))
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=CHAT + [left, right, editor, bubble]),
            driver=SimpleNamespace(type_text=AsyncMock(return_value=GENERATED_MESSAGE)),
            press_label=AsyncMock(), wait=AsyncMock(),
        )
        await send_qualified_message(session, GENERATED_MESSAGE, replace_draft="已有草稿")
        session.driver.type_text.assert_awaited_once_with(
            editor, GENERATED_MESSAGE, CARD.description, replace_draft="已有草稿")
        session.press_label.assert_awaited_once_with("发送")

    async def test_send_stops_if_chat_changed_after_typing_before_send(self):
        field = node("", "AXTextArea", path=(0, 9, 0))
        changed = CHAT[:-1] + [replace(CARD, description="另一人，沟通的职位-私域电商主播"),
                              replace(field, value=GENERATED_MESSAGE)]
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[CHAT + [field], changed]),
            driver=SimpleNamespace(set_value=AsyncMock()), press_label=AsyncMock(), wait=AsyncMock(),
        )
        with self.assertRaisesRegex(NativeDriverError, "会话候选人发生变化"):
            await send_qualified_message(session, GENERATED_MESSAGE)
        session.driver.set_value.assert_awaited_once_with(field, GENERATED_MESSAGE)
        session.press_label.assert_not_awaited()

    async def test_matching_draft_outside_history_cannot_confirm_send(self):
        field = node("", "AXTextArea", path=(0, 9, 0))
        written = CHAT + [replace(field, value=GENERATED_MESSAGE),
                          node(GENERATED_MESSAGE, "AXStaticText", path=(0, 9, 1))]
        async def wait(predicate, description):
            if "写入" in description:
                self.assertTrue(predicate(written))
                return written
            self.assertFalse(predicate(written))
            raise TimeoutError("no sent message in chat history")
        session = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[CHAT + [field], written]),
            driver=SimpleNamespace(set_value=AsyncMock()), press_label=AsyncMock(),
            wait=AsyncMock(side_effect=wait),
        )
        with self.assertRaisesRegex(TimeoutError, "no sent message"):
            await send_qualified_message(session, GENERATED_MESSAGE)
        session.press_label.assert_awaited_once_with("发送")

    async def test_message_confirmation_is_bound_to_original_chat(self):
        editor = node("和Ta聊一聊~", "AXGenericElement", path=(0, 9, 0))
        changed = CHAT[:-1] + [replace(CARD, description="另一人，沟通的职位-私域电商主播"),
                              node(GENERATED_MESSAGE, "AXStaticText", path=(0, 8, 2, 1))]
        async def wait(predicate, description):
            predicate(changed)
            self.fail("a changed conversation cannot confirm this send")
        session = SimpleNamespace(
            snapshot=AsyncMock(return_value=CHAT + [editor]),
            driver=SimpleNamespace(type_text=AsyncMock(return_value=GENERATED_MESSAGE)),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=wait),
        )
        with self.assertRaisesRegex(NativeDriverError, "会话候选人发生变化"):
            await send_qualified_message(session, GENERATED_MESSAGE)
        session.press_label.assert_awaited_once_with("发送")

    async def test_send_rejects_empty_or_multiline_generated_message(self):
        for message in ("", " ", "您好\n方便沟通吗", "您好\r方便沟通吗", "您好\u2028方便沟通吗"):
            with self.subTest(message=message), self.assertRaisesRegex(NativeDriverError, "单行文本"):
                await send_qualified_message(None, message)

    async def test_changed_resume_after_assessment_stops_before_back_or_send(self):
        changed_resume = [replace(RESUME[0], description="另一位候选人")] + RESUME[1:]
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock(), set_value=AsyncMock()),
            snapshot=AsyncMock(side_effect=[LIST, RESUME, changed_resume]),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(return_value="郑女士\n主播经验"), back=AsyncMock(),
        )
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch("native_greetings.send_saved_greeting", AsyncMock()) as send, \
             self.assertRaisesRegex(NativeDriverError, "候选人发生变化"):
            await loop_greetings(session, CONFIGS, None, {}, allow_contact=True)
        send.assert_not_awaited()
        session.back.assert_not_awaited()
        session.driver.set_value.assert_not_awaited()

    async def test_changed_chat_after_return_stops_before_send(self):
        changed_card = replace(CARD, description="另一位候选人，沟通的职位-私域电商主播")
        changed_chat = CHAT[:-1] + [changed_card]
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock(), set_value=AsyncMock()),
            snapshot=AsyncMock(side_effect=[LIST, RESUME, RESUME, changed_chat]),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[LIST, LIST, CHAT]),
            read_resume=AsyncMock(return_value="郑女士\n主播经验"), back=AsyncMock(return_value=changed_chat),
        )
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch("native_greetings.send_saved_greeting", AsyncMock()) as send, \
             self.assertRaisesRegex(NativeDriverError, "当前会话不是已评估"):
            await loop_greetings(session, CONFIGS, None, {}, allow_contact=True)
        send.assert_not_awaited()
        session.back.assert_awaited_once()
        session.driver.set_value.assert_not_awaited()

    async def test_existing_row_movement_does_not_prematurely_end_pagination(self):
        scroll = replace(node("", "AXGroup", path=(8,)), actions=("AXScrollDownByPage",))
        initial = LIST + [scroll]
        moved = LIST[:-1] + [replace(LIST[-1], rect={**LIST[-1].rect, "y": -200})] + [scroll]
        next_row = replace(LIST[-1], description="赖女士, [握手], 药业公司-药师, 大专 | 9000-10000元")
        next_page = LIST[:-1] + [next_row, scroll]
        session = SimpleNamespace(
            driver=SimpleNamespace(perform=AsyncMock()), snapshot=AsyncMock(return_value=initial),
            press_label=AsyncMock(), wait=AsyncMock(side_effect=[initial, initial, CHAT, moved, next_page, CHAT]),
            read_resume=AsyncMock(), back=AsyncMock(side_effect=[initial, EMPTY]),
        )
        processed = await loop_greetings(session, [{"job_title": "销售专员"}], None, {})
        self.assertEqual(processed, 0)
        self.assertEqual([call.args[1] for call in session.driver.perform.await_args_list],
                         ["press", "press", "press", "scrollDown", "scrollDown", "press"])
        session.read_resume.assert_not_awaited()


SAVED_ENTRY = node("常用语", path=(0, 1, 0, 9))
SAVED_ROW = node(SAVED_GREETING_MSG, "AXStaticText", path=(0, 1, 0, 14, 0, 0))
SAVED_SEND = node("发送", path=(0, 1, 0, 14, 0, 1))
SAVED_PANEL = CHAT + [node("常用语", "AXStaticText", path=(0, 1, 0, 13)),
                      node("键盘", path=(0, 1, 0, 12)), SAVED_ROW, SAVED_SEND,
                      node("其他常用回复", "AXStaticText", path=(0, 1, 0, 14, 1, 0)),
                      node("发送", path=(0, 1, 0, 14, 1, 1))]
SAVED_SENT = CHAT + [node(SAVED_GREETING_MSG, "AXStaticText", path=(0, 8, 2, 1))]


class SavedGreetingTests(unittest.IsolatedAsyncioTestCase):
    def session(self, snapshots):
        return SimpleNamespace(
            snapshot=AsyncMock(side_effect=snapshots),
            driver=SimpleNamespace(perform=AsyncMock(), set_value=AsyncMock(), type_text=AsyncMock()),
            press_label=AsyncMock(),
        )

    def assert_no_typing(self, session):
        session.driver.set_value.assert_not_awaited()
        session.driver.type_text.assert_not_awaited()
        session.press_label.assert_not_awaited()

    async def test_opens_panel_and_uses_only_exact_rows_adjacent_send_button(self):
        session = self.session([CHAT + [SAVED_ENTRY], SAVED_PANEL])
        async def wait(predicate, description):
            page = SAVED_PANEL if "面板" in description else SAVED_SENT
            self.assertTrue(predicate(page))
            return page
        session.wait = AsyncMock(side_effect=wait)
        await send_saved_greeting(session)
        self.assertEqual([call.args for call in session.driver.perform.await_args_list],
                         [(SAVED_ENTRY, "press"), (SAVED_SEND, "press")])
        self.assert_no_typing(session)

    async def test_open_panel_does_not_toggle_keyboard_or_modify_existing_draft(self):
        draft = node("未发送的草稿", "AXTextArea", value="未发送的草稿", path=(0, 9, 0))
        session = self.session([SAVED_PANEL + [draft], SAVED_PANEL + [draft]])
        session.wait = AsyncMock(return_value=SAVED_SENT)
        await send_saved_greeting(session)
        session.driver.perform.assert_awaited_once_with(SAVED_SEND, "press")
        self.assert_no_typing(session)

    async def test_existing_equal_history_message_needs_count_growth(self):
        old_message = node(SAVED_GREETING_MSG, "AXStaticText", path=(0, 8, 2, 1))
        panel = SAVED_PANEL + [old_message]
        session = self.session([panel, panel])
        async def wait(predicate, description):
            self.assertFalse(predicate(panel))
            sent = SAVED_SENT + [node(SAVED_GREETING_MSG, "AXStaticText", path=(0, 8, 3, 1))]
            self.assertTrue(predicate(sent))
            return sent
        session.wait = AsyncMock(side_effect=wait)
        await send_saved_greeting(session)
        session.driver.perform.assert_awaited_once_with(SAVED_SEND, "press")

    async def test_missing_or_duplicate_exact_saved_reply_stops_without_adding_it(self):
        missing = [replace(n, description="近似但不相同的常用语") if n == SAVED_ROW else n for n in SAVED_PANEL]
        duplicate = SAVED_PANEL + [replace(SAVED_ROW, path=(0, 1, 0, 14, 2, 0)),
                                   replace(SAVED_SEND, path=(0, 1, 0, 14, 2, 1))]
        for panel in (missing, duplicate):
            with self.subTest(panel=panel):
                session = self.session([panel, panel])
                with self.assertRaisesRegex(NativeDriverError, "请先在客户端保存完整文案"):
                    await send_saved_greeting(session)
                session.driver.perform.assert_not_awaited()
                self.assert_no_typing(session)

    async def test_duplicate_send_inside_target_row_is_not_guessed(self):
        panel = SAVED_PANEL + [replace(SAVED_SEND, path=(0, 1, 0, 14, 0, 2))]
        session = self.session([panel, panel])
        with self.assertRaisesRegex(NativeDriverError, "该条常用语的发送按钮"):
            await send_saved_greeting(session)
        session.driver.perform.assert_not_awaited()

    async def test_changed_candidate_before_send_stops(self):
        changed = [replace(n, description="另一位候选人，沟通的职位-私域电商主播") if n == CARD else n
                   for n in SAVED_PANEL]
        session = self.session([SAVED_PANEL, changed])
        with self.assertRaisesRegex(NativeDriverError, "会话候选人发生变化"):
            await send_saved_greeting(session)
        session.driver.perform.assert_not_awaited()

    async def test_changed_candidate_while_opening_panel_stops_before_send(self):
        changed = [replace(n, description="另一位候选人，沟通的职位-私域电商主播") if n == CARD else n
                   for n in SAVED_PANEL]
        session = self.session([CHAT + [SAVED_ENTRY]])
        async def wait(predicate, description):
            predicate(changed)
        session.wait = AsyncMock(side_effect=wait)
        with self.assertRaisesRegex(NativeDriverError, "会话候选人发生变化"):
            await send_saved_greeting(session)
        session.driver.perform.assert_awaited_once_with(SAVED_ENTRY, "press")

    async def test_unconfirmed_send_stops_without_retry_and_draft_cannot_confirm(self):
        session = self.session([SAVED_PANEL, SAVED_PANEL])
        draft_only = CHAT + [node(SAVED_GREETING_MSG, "AXStaticText", path=(0, 9, 1))]
        async def wait(predicate, description):
            self.assertFalse(predicate(SAVED_PANEL))
            self.assertFalse(predicate(draft_only))
            raise TimeoutError("sent history not confirmed")
        session.wait = AsyncMock(side_effect=wait)
        with self.assertRaisesRegex(TimeoutError, "not confirmed"):
            await send_saved_greeting(session)
        session.driver.perform.assert_awaited_once_with(SAVED_SEND, "press")
        self.assert_no_typing(session)

    async def test_other_candidates_message_cannot_confirm_submission(self):
        session = self.session([SAVED_PANEL, SAVED_PANEL])
        changed = [replace(n, description="另一位候选人，沟通的职位-私域电商主播") if n == CARD else n
                   for n in SAVED_SENT]
        async def wait(predicate, description):
            predicate(changed)
        session.wait = AsyncMock(side_effect=wait)
        with self.assertRaisesRegex(NativeDriverError, "会话候选人发生变化"):
            await send_saved_greeting(session)
        session.driver.perform.assert_awaited_once_with(SAVED_SEND, "press")


class GreetingNavigationTests(unittest.IsolatedAsyncioTestCase):
    def checked_session(self, driver):
        from native_workflow import NativeSession
        session = NativeSession(driver)
        async def wait(predicate, description, **options):
            page = await session.snapshot(allow_network_error=options.get("allow_network_error", False))
            self.assertTrue(predicate(page), description)
            return page
        session.wait = AsyncMock(side_effect=wait)
        return session

    async def test_list_banner_allows_navigation_but_chat_banner_stops_before_resume(self):
        banner = node("网络连接失败，请检查网络")
        messages = [node("消息"), node("新招呼"), banner]
        page = [node("消息"), node("牛人"), banner]
        async def perform(control, action):
            nonlocal page
            if control.description == "消息":
                page = messages
            elif control.description in ("新招呼", "未读"):
                page = LIST + [banner]
            else:
                self.assertEqual(control, LIST[-1])
                page = CHAT + [banner]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=lambda: page), perform=AsyncMock(side_effect=perform))
        session = self.checked_session(driver)
        session.read_resume = AsyncMock()
        with self.assertRaisesRegex(NativeDriverError, "网络连接失败"):
            await loop_greetings(session, CONFIGS, None, {})
        self.assertEqual([call.args[0].description for call in driver.perform.await_args_list],
                         ["消息", "新招呼", "未读", LIST[-1].description])
        session.read_resume.assert_not_awaited()
        self.assertTrue(all(call.kwargs.get("allow_network_error") is True
                            for call in session.wait.await_args_list[:-1]))
        self.assertNotIn("allow_network_error", session.wait.await_args_list[-1].kwargs)

    async def test_navigation_never_ignores_login_or_face_verification(self):
        for text in ("验证码登录", "人脸验证"):
            driver = SimpleNamespace(snapshot=AsyncMock(return_value=LIST + [node("网络连接失败"), node(text)]),
                                     perform=AsyncMock())
            with self.subTest(text=text), self.assertRaisesRegex(NativeDriverError, "人工处理"):
                await loop_greetings(self.checked_session(driver), CONFIGS, None, {})
            driver.perform.assert_not_awaited()

    async def test_two_replies_refresh_list_and_do_not_reopen_first_candidate(self):
        banner = node("网络连接失败，请检查网络")
        row_b = replace(LIST[-1], description="赖女士, 应聘主播, 药业公司-主播, 大专 | 9000-10000元")
        cards = [CARD, replace(CARD, description="赖女士，沟通的职位-私域电商主播")]
        chats = [CHAT, CHAT[:-1] + [cards[1]]]
        resumes = [RESUME, [replace(RESUME[0], description="赖女士")] + RESUME[1:]]
        lists = [LIST + [banner], LIST + [row_b, banner], EMPTY + [banner]]
        page = lists[0]
        current = 0
        async def perform(control, action):
            nonlocal page, current
            if control.description in ("新招呼", "未读"):
                return
            if control in (LIST[-1], row_b):
                current = 0 if control == LIST[-1] else 1
                page = chats[current]
            else:
                self.assertEqual(control, cards[current])
                page = resumes[current]
        driver = SimpleNamespace(snapshot=AsyncMock(side_effect=lambda: page), perform=AsyncMock(side_effect=perform))
        session = self.checked_session(driver)
        session.read_resume = AsyncMock(side_effect=["郑女士\n工作经历\n主播经验", "赖女士\n工作经历\n主播经验"])
        async def back(predicate, description, **options):
            nonlocal page
            page = chats[current] if "关闭" in description else lists[current+1]
            return await session.wait(predicate, description, **options)
        session.back = AsyncMock(side_effect=back)
        stats = {}
        with patch("native_greetings.llm_utils.is_qualified_text_result", return_value=SimpleNamespace(is_qualified=True)), \
             patch("native_greetings.send_saved_greeting", new=AsyncMock()) as send:
            self.assertEqual(await loop_greetings(session, CONFIGS, None, stats, allow_contact=True), 2)
        self.assertEqual(stats["私域电商主播"]["requested"], 2)
        self.assertEqual(stats["私域电商主播"]["inbound_viewed"], 2)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(session.read_resume.await_count, 2)
        self.assertEqual([call.args[0] for call in driver.perform.await_args_list[2:]],
                         [LIST[-1], cards[0], row_b, cards[1]])
        list_returns = [call for call in session.back.await_args_list if "返回新招呼列表" in call.args[1]]
        self.assertEqual(len(list_returns), 2)
        self.assertTrue(all(call.kwargs == {"allow_network_error": True} for call in list_returns))


if __name__ == "__main__":
    unittest.main()
