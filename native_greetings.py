"""Inbound greeting screening through the native iOS application's AX tree."""
import asyncio
import re

import job_utils
import llm_utils
from log_utils import logger
from native_driver import NativeDriverError, NativeNetworkError


GREETING_QUALIFIED_MSG = "您好，感谢您的主动联系！您的背景很符合我们的要求，麻烦发一份简历给我看看~"
SAVED_GREETING_MSG = "您好，感谢您的主动联系！方便发一份完整简历，进一步了解一下您的相关经历吗？"


def match_greeting_job(title, job_configs):
    """Match an actual chat position to exactly one configured position."""
    matches = []
    for config in job_configs:
        selector = config.get("job_requirements", {}).get("selector_job_title")
        names = [config["job_title"], selector] if selector else [config["job_title"]]
        if any(title == name or re.fullmatch(re.escape(name) + r"[（(].*[）)]", title) for name in names):
            matches.append(config)
    if len(matches) > 1:
        raise NativeDriverError(f"新招呼职位匹配到多个配置：{title}")
    return matches[0] if matches else None


def chat_position(nodes):
    cards = [node for node in nodes if "沟通的职位-" in node.description and "AXPress" in node.actions]
    if len(cards) != 1:
        raise NativeDriverError("无法唯一识别当前聊天的简历卡片及沟通职位")
    title = cards[0].description.rsplit("沟通的职位-", 1)[1].strip()
    if not title:
        raise NativeDriverError("当前聊天的沟通职位为空")
    return title, cards[0]


def _message_count(nodes, text, expected_card_description):
    _, card = chat_position(nodes)
    if card.description != expected_card_description:
        raise NativeDriverError("发送期间会话候选人发生变化，停止发送")
    history = card.path[:-2]
    if not history:
        raise NativeDriverError("无法确定会话消息区域，停止发送")
    return sum(n.role == "AXStaticText" and n.path[:len(history)] == history
               and n.description == text for n in nodes)


async def send_saved_greeting(session, text=SAVED_GREETING_MSG):
    """Send one existing exact-match saved reply using its own row's button."""
    if not text.strip() or any(ord(c) < 32 or c in "\u2028\u2029" for c in text):
        raise NativeDriverError("常用语必须是非空的单行文本")
    nodes = await session.snapshot()
    _, expected_card = chat_position(nodes)

    def panel_open(ns):
        _message_count(ns, text, expected_card.description)
        return (any(n.role == "AXStaticText" and n.description == "常用语" for n in ns)
                and any(n.role == "AXButton" and n.description == "键盘" and "AXPress" in n.actions for n in ns))

    if not panel_open(nodes):
        entries = [n for n in nodes if n.role == "AXButton" and n.description == "常用语" and "AXPress" in n.actions]
        if len(entries) != 1:
            raise NativeDriverError("无法唯一定位常用语入口，停止发送")
        await session.driver.perform(entries[0], "press")
        await session.wait(panel_open, "常用语面板")

    # Resolve the saved row and candidate from a fresh snapshot before writing.
    nodes = await session.snapshot()
    before = _message_count(nodes, text, expected_card.description)
    if not panel_open(nodes):
        raise NativeDriverError("常用语面板状态发生变化，停止发送")
    _, current_card = chat_position(nodes)
    history = current_card.path[:-2]
    rows = [n for n in nodes if n.role == "AXStaticText" and n.description == text
            and n.path[:len(history)] != history]
    if len(rows) != 1:
        raise NativeDriverError(f"未找到唯一匹配的常用语，请先在客户端保存完整文案：{text}")
    sends = [n for n in nodes if n.role == "AXButton" and n.description == "发送" and "AXPress" in n.actions
             and n.path[:-1] == rows[0].path[:-1]]
    if len(sends) != 1:
        raise NativeDriverError("无法唯一定位该条常用语的发送按钮，停止发送")
    await session.driver.perform(sends[0], "press")
    await session.wait(
        lambda ns: _message_count(ns, text, expected_card.description) > before,
        "固定常用语发送确认（未确认时停止，不重试发送）",
    )
    logger.info("固定常用语已发送，并在当前会话历史中确认")


async def send_qualified_message(session, text=GREETING_QUALIFIED_MSG, *, replace_draft=None):
    """Send once, then require the exact message to appear in the conversation."""
    if not text.strip() or any(ord(c) < 32 or c in "\u2028\u2029" for c in text):
        raise NativeDriverError("招呼消息必须是非空的单行文本")
    nodes = await session.snapshot()
    _, expected_card = chat_position(nodes)

    def message_count(ns):
        return _message_count(ns, text, expected_card.description)

    before = message_count(nodes)
    fields = [node for node in nodes if node.role in ("AXTextArea", "AXTextField")]
    if not fields:
        editors = [n for n in nodes if n.role == "AXGenericElement" and n.description == "和Ta聊一聊~"]
        if not editors:
            # UIKit exposes a nonempty composer as AXStaticText. Locate that
            # editor between its actual toolbar controls, not by draft wording.
            left = [n for n in nodes if n.role == "AXButton" and n.description == "常用语"]
            right = [n for n in nodes if n.role == "AXButton" and n.description == "表情"]
            if len(left) == len(right) == 1:
                baseline = left[0].rect["y"] + left[0].rect["height"] / 2
                history = expected_card.path[:-2]
                editors = [n for n in nodes if n.role == "AXStaticText" and "AXPress" in n.actions
                           and n.path[:len(history)] != history
                           and n.rect["x"] >= left[0].rect["x"] + left[0].rect["width"]
                           and n.rect["x"] + n.rect["width"] <= right[0].rect["x"]
                           and n.rect["y"] <= baseline <= n.rect["y"] + n.rect["height"]]
        if len(editors) != 1:
            raise NativeDriverError("无法唯一识别聊天输入框，停止发送")
        draft_options = {"replace_draft": replace_draft} if replace_draft is not None else {}
        written = await session.driver.type_text(editors[0], text, expected_card.description, **draft_options)
        if written != text:
            raise NativeDriverError("聊天输入内容未通过核对，停止发送")
    else:
        if len(fields) != 1:
            raise NativeDriverError("无法唯一识别聊天输入框，停止发送")
        if fields[0].value:
            raise NativeDriverError("聊天输入框已有内容，停止发送")
        await session.driver.set_value(fields[0], text)
        await session.wait(
            lambda ns: any(n.role in ("AXTextArea", "AXTextField") and n.value == text for n in ns),
            "招呼文本写入确认",
        )
    message_count(await session.snapshot())
    await session.press_label("发送")
    await session.wait(
        lambda ns: message_count(ns) > before,
        "招呼消息发送确认（未确认时停止，不重试发送）",
    )


MARKED_UNSUITABLE_LABELS = ("已标记不合适", "已标为不合适")
UNSUITABLE_REASON_HEADERS = ("选择原因，推荐更合适牛人", "选择原因，为您优化推荐")
# The native recommendation panel offers candidate-specific presets instead of
# the model's fixed categories. Map by semantic hint, then fall back to 其他原因.
PLATFORM_REASON_HINTS = {
    "薪资不符": ("薪资",),
    "学历不符": ("不考虑", "学历"),
    "年龄不符": ("年龄",),
    "期望不符": ("期望薪资", "薪资", "期望"),
    "距离太远": ("距离",),
    "过往经历不符": ("工作经历", "经历"),
    "简历不真实": ("简历", "真实"),
    "已找到工作": ("已找到工作",),
    "其他原因": ("其他原因",),
}
REASON_LABEL_HINTS = ("薪资", "学历", "年龄", "距离", "经历", "简历",
                      "已找到", "重复推荐", "活跃", "不考虑", "其他原因")
NON_REASON_LABELS = {"求职期望", "工作经历", "项目经历", "教育经历", "最近关注", "牛人分析器"}


def is_marked_unsuitable(nodes):
    from native_workflow import has_label

    return any(has_label(nodes, text) for text in MARKED_UNSUITABLE_LABELS)


def is_reason_label(text):
    if text in NON_REASON_LABELS:
        return False
    return text in llm_utils.DISQUALIFICATION_REASON_CATEGORIES or any(
        hint in text for hint in REASON_LABEL_HINTS
    )


def visible_reason_labels(nodes):
    from native_workflow import label

    return [label(n) for n in nodes if n.role == "AXStaticText" and "AXPress" in n.actions
            and is_reason_label(label(n))]


def platform_reason_label(nodes, reason_category):
    """Choose exactly one visible platform preset for the model's category."""
    if reason_category not in llm_utils.DISQUALIFICATION_REASON_CATEGORIES:
        raise NativeDriverError(f"无效的不合适原因：{reason_category}")
    visible = visible_reason_labels(nodes)
    if reason_category in visible:
        return reason_category
    for hint in PLATFORM_REASON_HINTS.get(reason_category, ()):
        matches = [text for text in visible if hint in text]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise NativeDriverError(f"不合适原因不唯一：{reason_category} -> {hint}")
    if "其他原因" in visible:
        return "其他原因"
    raise NativeDriverError(f"客户端未提供匹配的淘汰原因：{reason_category}")


def top_right_menu_button(nodes):
    """Identify the rightmost toolbar button, which opens the detail menu."""
    from native_workflow import label

    window = next((n.rect for n in nodes if n.role == "AXWindow"), nodes[0].rect)
    buttons = [n for n in nodes if n.role == "AXButton" and "AXPress" in n.actions
               and n.rect["height"] > 20
               and n.rect["x"] > window["x"] + window["width"] - 120
               and window["y"] + 25 <= n.rect["y"] < window["y"] + 100]
    if not buttons:
        raise NativeDriverError("无法定位详情页更多按钮")
    return max(buttons, key=lambda n: n.rect["x"])


async def open_detail_unsuitable_panel(session):
    """Open 不合适 from the recommendation detail toolbar."""
    from native_workflow import has_label, label

    nodes = await session.snapshot()
    direct = [n for n in nodes if label(n) == "不合适" and "AXPress" in n.actions]
    if len(direct) == 1:
        await session.driver.perform(direct[0], "press")
        return
    if len(direct) > 1:
        raise NativeDriverError("详情页不合适入口必须唯一")
    await session.driver.perform(top_right_menu_button(nodes), "press")
    nodes = await session.wait(lambda ns: has_label(ns, "不合适"), "详情页更多菜单")
    direct = [n for n in nodes if label(n) == "不合适" and "AXPress" in n.actions]
    if len(direct) != 1:
        raise NativeDriverError("详情页不合适菜单项必须唯一")
    await session.driver.perform(direct[0], "press")


async def mark_unsuitable(session, reason_category, expected_card_description=None, *,
                          verify_page=None, success_check=None, open_panel=None,
                          map_reason=None, advance_check=None, reason_text=None):
    """Choose a reason and submit once, requiring explicit same-person success.

    The chat flow binds to the visible conversation card. The recommendation
    flow binds to the evaluated resume page, opens the toolbar menu, and also
    accepts the client's post-submit return to the candidate list.
    """
    if reason_category not in llm_utils.DISQUALIFICATION_REASON_CATEGORIES:
        raise NativeDriverError(f"无效的不合适原因：{reason_category}")
    from native_workflow import has_label, label

    def panel_open(ns):
        return any(has_label(ns, text) for text in UNSUITABLE_REASON_HEADERS)

    confirm_labels = ("确定", "确认", "确认不合适", "确定不合适")

    if verify_page is None:
        if expected_card_description is None:
            raise ValueError("mark_unsuitable requires a page binding")
        def verify_page(ns):
            _, card = chat_position(ns)
            if card.description != expected_card_description:
                raise NativeDriverError("不合适操作期间会话候选人发生变化，停止提交")

    def marked(ns):
        return is_marked_unsuitable(ns)

    # Success must be explicit: a visible marker or a caller-verified transition.
    # It is checked before binding because the client may leave the chat/detail
    # immediately after submitting the reason.
    if success_check is None:
        def success_check(ns):
            verify_page(ns)
            return marked(ns)

    def confirmations(ns):
        return [n for n in ns if n.role == "AXButton" and label(n) in confirm_labels and "AXPress" in n.actions]

    custom_text = re.sub(r"\s+", " ", reason_text or reason_category).strip()[:50]
    if not custom_text:
        raise NativeDriverError("自定义不合适原因不能为空")

    def custom_dialog_open(ns):
        return (has_label(ns, "输入原因")
                and any(n.role in ("AXTextArea", "AXTextField") for n in ns)
                and any(n.role == "AXButton" and label(n) == "确定" and "AXPress" in n.actions
                        for n in ns))

    async def submit_custom_reason(ns):
        fields = [n for n in ns if n.role in ("AXTextArea", "AXTextField")]
        if len(fields) != 1:
            raise NativeDriverError("自定义不合适原因输入框必须唯一")
        logger.llm(f"不合适原因：客户端要求自定义原因，写入 {custom_text}")
        await session.driver.set_value(fields[0], custom_text)
        updated = await session.wait(
            lambda current: any(
                n.role in ("AXTextArea", "AXTextField") and n.value == custom_text
                for n in current
            ), "自定义不合适原因写入确认",
        )
        confirms = [n for n in updated if n.role == "AXButton" and label(n) == "确定"
                    and "AXPress" in n.actions]
        if len(confirms) != 1:
            raise NativeDriverError("自定义不合适原因确定按钮必须唯一")
        await session.driver.perform(confirms[0], "press")

    def reason_context(ns, reason_label):
        # UIKit can expose selection through AXValue, or replace the reason list
        # with a confirmation summary containing only the chosen preset reason.
        selected = {label(n) for n in ns if label(n) == reason_label
                    and n.value in ("选中", "已选中", "1")}
        if selected == {reason_label}:
            return True
        reasons = {text for text in visible_reason_labels(ns)}
        if not panel_open(ns) and reasons == {reason_label}:
            return True
        return any(n.role == "AXStaticText" and label(n) != reason_label and reason_label in label(n)
                   and ("原因" in label(n) or "不合适" in label(n))
                   and not any(other in label(n) for other in visible_reason_labels(ns)
                               if other != reason_label)
                   for n in ns)

    nodes = await session.snapshot()
    verify_page(nodes)
    if marked(nodes):
        raise NativeDriverError("当前候选人已标记不合适，不重复提交")
    if not panel_open(nodes):
        if open_panel is None:
            await session.press_label("不合适")
        else:
            await open_panel(session)
        nodes = await session.wait(
            lambda ns: panel_open(ns) or success_check(ns), "不合适原因面板"
        )
        if success_check(nodes):
            return
    verify_page(nodes)
    if confirmations(nodes) or any(label(n) in llm_utils.DISQUALIFICATION_REASON_CATEGORIES
                                   and n.value in ("选中", "已选中", "1") for n in nodes):
        raise NativeDriverError("不合适面板已有原因选择，停止以保留当前选择")
    if not visible_reason_labels(nodes):
        nodes = await session.wait(
            lambda ns: bool(visible_reason_labels(ns)) or success_check(ns),
            "不合适原因列表",
        )
        if success_check(nodes):
            return
    # The chat panel uses the model's fixed categories. The recommendation
    # panel offers candidate-specific presets, so its caller supplies a mapper.
    reason_label = reason_category if map_reason is None else map_reason(nodes)
    logger.llm(f"不合适原因：模型分类 {reason_category} -> 客户端选项 {reason_label}")
    choices = [n for n in nodes if label(n) == reason_label and "AXPress" in n.actions]
    if len(choices) != 1:
        raise NativeDriverError(f"不合适原因必须唯一：{reason_label}")
    await session.driver.perform(choices[0], "press")

    def advanced(ns):
        # After the reason is submitted the client may auto-open the next
        # candidate instead of returning to a list. Only accept that after a
        # reason press, never before it.
        return advance_check is not None and advance_check(ns)

    def submitted(ns):
        return success_check(ns) or advanced(ns)

    def confirmation_ready(ns):
        # A preset reason can submit immediately in some client versions; the
        # success marker, caller-verified transition, or custom-reason dialog
        # must win over binding.
        if submitted(ns) or custom_dialog_open(ns):
            return True
        verify_page(ns)
        return len(confirmations(ns)) == 1 and reason_context(ns, reason_label)

    nodes = await session.wait(confirmation_ready, "不合适原因选择及确认按钮")
    if submitted(nodes):
        return
    if custom_dialog_open(nodes):
        await submit_custom_reason(nodes)
        await session.wait(submitted, "不合适标记成功（未确认时停止，不重试提交）")
        return
    # Resolve the confirmation again rather than reusing a node from before a
    # transition; the candidate and selected reason must still match now.
    nodes = await session.snapshot()
    verify_page(nodes)
    confirms = confirmations(nodes)
    if len(confirms) != 1 or not reason_context(nodes, reason_label):
        raise NativeDriverError("无法确认当前不合适原因与提交按钮，停止提交")
    await session.driver.perform(confirms[0], "press")

    await session.wait(submitted, "不合适标记成功（未确认时停止，不重试提交）")


def greeting_rows(nodes):
    """Recognize observed message rows; the target job exists only in chat."""
    return [n for n in nodes if n.role == "AXStaticText" and "AXPress" in n.actions
            and n.description.count(",") >= 3 and " | " in n.description]


async def loop_greetings(session, job_configs, client, job_stats,
                         allow_contact=False, max_candidates=200, rejection_reviewer=None):
    """Read new greetings and evaluate resumes without contacting by default.

    Contact actions require explicit opt-in. Rejection must expose the selected
    reason, confirmation control and explicit same-person success state.
    """
    from native_workflow import has_label, is_candidate_list, is_resume, label, page_identity

    def is_chat(ns):
        return has_label(ns, "求简历") and has_label(ns, "换电话/微信")

    def is_greetings(ns):
        return has_label(ns, "新招呼") and has_label(ns, "批量处理") and not is_chat(ns)

    async def navigate(ns, text, predicate, description):
        controls = [n for n in ns if label(n) == text and "AXPress" in n.actions]
        if len(controls) != 1:
            raise NativeDriverError(f"导航控件必须唯一：{text!r}，实际 {len(controls)} 个")
        await session.driver.perform(controls[0], "press")
        return await session.wait(predicate, description, allow_network_error=True)

    # A stale connection banner on message lists must not block navigation.
    # Conversation entry, resume reading and all writes keep strict checks.
    nodes = await session.snapshot(allow_network_error=True)
    if is_chat(nodes):
        nodes = await session.back(lambda ns: has_label(ns, "消息"), "消息列表", allow_network_error=True)
    elif is_resume(nodes):
        nodes = await session.back(lambda ns: is_chat(ns) or is_candidate_list(ns), "退出简历", allow_network_error=True)
        if is_chat(nodes):
            nodes = await session.back(lambda ns: has_label(ns, "消息"), "消息列表", allow_network_error=True)
    if not is_greetings(nodes):
        if not has_label(nodes, "消息"):
            raise NativeDriverError("无法从当前客户端页面进入新招呼列表")
        nodes = await navigate(nodes, "消息", lambda ns: has_label(ns, "新招呼"), "消息页新招呼入口")
    nodes = await navigate(nodes, "新招呼", is_greetings, "新招呼列表")
    if not has_label(nodes, "未读"):
        logger.info("新招呼没有未读会话，结束本轮处理")
        return 0
    nodes = await navigate(nodes, "未读", is_greetings, "新招呼未读列表")

    seen = set()
    processed = 0
    while len(seen) < max_candidates:
        if any(has_label(nodes, text) for text in ("暂无新招呼", "暂无消息", "暂无新消息", "暂时没有新招呼")):
            break
        rows = greeting_rows(nodes)
        row = next((r for r in rows if r.description not in seen), None)
        if row is None:
            scrolls = [n for n in nodes if n.role == "AXGroup" and "AXScrollDownByPage" in n.actions]
            if not rows and not scrolls:
                raise NativeDriverError("新招呼列表未提供已验证的候选人行，停止处理")
            if not scrolls:
                break
            before = tuple((r.description, round(r.rect["y"], 1)) for r in rows)
            await session.driver.perform(scrolls[0], "scrollDown")
            nodes = await session.wait(is_greetings, "新招呼翻页", stable_for=1, allow_network_error=True)
            if tuple((r.description, round(r.rect["y"], 1)) for r in greeting_rows(nodes)) == before:
                if not rows:
                    raise NativeDriverError("无法识别新招呼候选人行，停止处理")
                break
            continue
        seen.add(row.description)
        await session.driver.perform(row, "press")
        chat_nodes = await session.wait(is_chat, "新招呼会话")
        actual_title, card = chat_position(chat_nodes)
        config = match_greeting_job(actual_title, job_configs)
        if config is None:
            logger.info("跳过未配置的新招呼职位：%s", actual_title)
            nodes = await session.back(is_greetings, "跳过后返回新招呼列表", allow_network_error=True)
            continue
        requirements = job_utils.get_job_requirements(config.get("job_requirements", {}))
        candidate_label = row.description.split(",", 1)[0]
        # 会话里的卡片只是详情入口；先打开完整简历，不能拿卡片摘要代替评估材料。
        logger.info("新招呼[%s / %s]：打开详细简历", actual_title, candidate_label)
        await session.driver.perform(card, "press")
        try:
            text = await session.read_resume()
        except TimeoutError:
            await session.back(is_chat, "超时后关闭在线简历")
            await session.back(is_greetings, "超时后返回新招呼列表", allow_network_error=True)
            nodes = await session.snapshot(allow_network_error=True)
            logger.warning("新招呼简历加载超时，已返回列表")
            continue
        except NativeNetworkError:
            # Same safe recovery as a timeout: prove the chat and greetings
            # list are back, then skip this candidate without retrying the read.
            await session.back(is_chat, "网络横幅后关闭在线简历", allow_network_error=True)
            nodes = await session.back(is_greetings, "网络横幅后返回新招呼列表",
                                       allow_network_error=True)
            logger.warning("新招呼简历读取遇到网络横幅，已确认返回列表，跳过当前候选人")
            continue
        logger.info("新招呼[%s / %s]：详细简历读取完成，共 %s 字", actual_title, candidate_label, len(text))
        assessed_page = page_identity(await session.snapshot()) if allow_contact else None
        result = await asyncio.to_thread(
            llm_utils.is_qualified_text_result, client, text, requirements["cv_requirements"]
        )
        if allow_contact:
            current = await session.snapshot()
            if not is_resume(current) or page_identity(current) != assessed_page:
                raise NativeDriverError("评估期间新招呼简历或候选人发生变化，停止处理")
        await session.back(is_chat, "关闭新招呼在线简历")
        logger.info("新招呼[%s / %s]：关闭详情，已返回会话", actual_title, candidate_label)
        counters = job_stats.setdefault(config["job_title"], {})
        counters["inbound_viewed"] = counters.get("inbound_viewed", 0) + 1
        marked_now = False
        if result is None:
            logger.warning("新招呼 LLM 评估失败，跳过当前候选人")
        elif result.is_qualified:
            counters["inbound_qualified"] = counters.get("inbound_qualified", 0) + 1
            if allow_contact:
                current_title, current_card = chat_position(await session.snapshot())
                if current_title != actual_title or current_card.description != card.description:
                    raise NativeDriverError("当前会话不是已评估的新招呼候选人，停止发送")
                await send_saved_greeting(session)
                counters["requested"] = counters.get("requested", 0) + 1
            else:
                logger.info("新招呼候选人符合要求；只读模式，未发送消息")
        elif allow_contact:
            # 默认按模型结论自动标记；显式开启复核时逐人确认，未获确认的结论保留待复核。
            approved = True
            if rejection_reviewer is not None:
                approved = await rejection_reviewer(config["job_title"], result)
            if approved is True:
                def verify_chat(ns):
                    if is_marked_unsuitable(ns):
                        return
                    _, current_card = chat_position(ns)
                    if current_card.description != card.description:
                        raise NativeDriverError("不合适操作期间会话候选人发生变化，停止提交")

                def marked_or_left_chat(ns):
                    # 客户端提交后可能直接回到新招呼列表；两种状态都只由本次
                    # 已确认的提交引起，其他结果继续等待或停止。
                    return is_marked_unsuitable(ns) or is_greetings(ns)

                await mark_unsuitable(
                    session, result.reason_category,
                    verify_page=verify_chat, success_check=marked_or_left_chat,
                )
                marked_now = True
                counters["marked_unsuitable"] = counters.get("marked_unsuitable", 0) + 1
                logger.info("新招呼候选人不符合要求，已标记不合适：%s", config["job_title"])
            else:
                counters["awaiting_review"] = counters.get("awaiting_review", 0) + 1
                counters.setdefault("pending_rejections", []).append({
                    "reason": result.reason,
                    "reason_category": result.reason_category,
                    "expected_card_description": card.description,
                })
                logger.info("新招呼淘汰结论待人工复核，未标记不合适：%s", config["job_title"])
        else:
            logger.info("新招呼候选人不符合要求；只读模式，未标记不合适")
        processed += 1
        if marked_now:
            # Submission can land back on the greetings list; only then skip
            # the back action, which requires a chat page with a leading arrow.
            current = await session.snapshot(allow_network_error=True)
            if is_greetings(current):
                nodes = current
                continue
        nodes = await session.back(is_greetings, "返回新招呼列表", allow_network_error=True)
    return processed
