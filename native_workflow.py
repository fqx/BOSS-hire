"""Screen resumes in the iOS BOSS app using macOS accessibility."""
import asyncio
import re
import time

import job_utils
import llm_utils
from log_utils import logger
from timing_utils import jitter
from native_driver import NativeDriverError, NativeStaleElementError


def label(node):
    return node.description or node.title or node.value


def has_label(nodes, text):
    return any(label(node) == text for node in nodes)


def is_candidate_list(nodes):
    return has_label(nodes, "牛人") and has_label(nodes, "推荐")


def is_resume(nodes):
    metadata = has_label(nodes, "求职期望") and any(
        has_label(nodes, text) for text in ("工作经历", "教育经历")
    )
    # Long native resumes virtualize their cells: the expectation heading leaves
    # the AX tree after scrolling, while the bottom contact controls remain.
    contact = any(n.role == "AXButton" and label(n) in ("立即沟通", "继续沟通")
                  and "AXPress" in n.actions for n in nodes)
    return metadata or (contact and not has_label(nodes, "牛人")
                        and not has_label(nodes, "换电话/微信"))


def page_identity(nodes):
    """Bind a pending write to the page whose resume was evaluated."""
    return tuple((n.path, n.role, label(n)) for n in nodes
                 if n.role in ("AXStaticText", "AXButton") and label(n)
                 and not label(n).startswith("“不合适、举报”功能"))


class NativeSession:
    def __init__(self, driver):
        self.driver = driver

    async def snapshot(self, *, allow_network_error=False):
        nodes = await self.driver.snapshot()
        # Inspect dedicated UI labels rather than candidate prose or chat history.
        for node in nodes:
            text = label(node)
            if not allow_network_error and node.role == "AXButton" and text.startswith("网络连接失败"):
                raise NativeDriverError("BOSS 网络连接失败，请先恢复客户端网络")
            if text in ("安全验证", "人脸验证", "身份验证", "账号异常", "登录/注册", "验证码登录"):
                raise NativeDriverError(f"BOSS 需要人工处理：{text}")
        return nodes

    async def wait(self, predicate, description, timeout=15, stable_for=0.5, *, allow_network_error=False):
        deadline = time.monotonic() + timeout
        previous = None
        stable_since = None
        while time.monotonic() < deadline:
            nodes = await self.snapshot(allow_network_error=allow_network_error)
            signature = tuple((n.path, n.role, label(n), n.value,
                               tuple(round(n.rect[k], 1) for k in ("x", "y", "width", "height")))
                              for n in nodes)
            if predicate(nodes):
                if signature != previous:
                    stable_since = time.monotonic()
                if stable_since is not None and time.monotonic() - stable_since >= stable_for:
                    return nodes
                previous = signature
            else:
                previous = stable_since = None
            await asyncio.sleep(jitter(0.2))
        raise TimeoutError(f"等待客户端页面超时：{description}")

    async def press_label(self, text):
        nodes = await self.snapshot()
        matches = [n for n in nodes if label(n) == text and "AXPress" in n.actions]
        if len(matches) != 1:
            raise NativeDriverError(f"控件必须唯一：{text!r}，实际 {len(matches)} 个")
        await self.driver.perform(matches[0], "press")

    async def back(self, predicate, description, *, allow_network_error=False):
        navigation_options = {"allow_network_error": True} if allow_network_error else {}
        nodes = await self.snapshot(**navigation_options)
        original_page = page_identity(nodes)
        for attempt in range(2):
            window = nodes[0].rect
            # UIKit exposes its back arrow without a label (or with an unread badge).
            # Identify the small leading navigation button, excluding macOS chrome.
            buttons = [n for n in nodes if n.role == "AXButton" and "AXPress" in n.actions
                       and n.rect["height"] > 20
                       and 0 < n.rect["width"] < 100
                       and window["x"] <= n.rect["x"] < window["x"] + 70
                       and window["y"] + 25 <= n.rect["y"] < window["y"] + 100]
            if len(buttons) != 1:
                raise NativeDriverError("无法唯一定位客户端返回按钮")
            await self.driver.perform(buttons[0], "press")
            try:
                return await self.wait(predicate, description, timeout=5 if attempt == 0 else 15,
                                       **navigation_options)
            except TimeoutError:
                nodes = await self.snapshot(**navigation_options)
                if predicate(nodes):
                    return await self.wait(predicate, description, **navigation_options)
                # The native button can acknowledge AXPress without navigating.
                # Retry once only if we can still identify the original page.
                if attempt or page_identity(nodes) != original_page:
                    raise
                logger.warning("返回操作未生效，确认仍在原页面后重试一次")

    async def return_to_candidates(self):
        nodes = await self.snapshot()
        if is_candidate_list(nodes):
            return nodes
        if not is_resume(nodes) and not has_label(nodes, "求简历"):
            raise NativeDriverError("当前不是简历或会话页面，无法返回候选人列表")
        nodes = await self.back(lambda ns: is_resume(ns) or is_candidate_list(ns), "简历或候选人列表")
        if is_candidate_list(nodes):
            return nodes
        return await self.back(is_candidate_list, "候选人列表")

    async def select_job(self, job_title, selector_job_title=None):
        # Message delivery can be offline while recommendations remain usable.
        # Permit only navigation out of that page, then recheck the destination.
        nodes = await self.snapshot(allow_network_error=True)
        if is_resume(nodes):
            nodes = await self.back(lambda ns: is_candidate_list(ns) or has_label(ns, "求简历"),
                                    "退出在线简历", allow_network_error=True)
        if has_label(nodes, "求简历") and has_label(nodes, "换电话/微信"):
            nodes = await self.back(lambda ns: has_label(ns, "牛人") and has_label(ns, "消息"),
                                    "返回客户端导航", allow_network_error=True)
        if not is_candidate_list(nodes):
            buttons = [n for n in nodes if n.role == "AXButton" and label(n) == "牛人"
                       and "AXPress" in n.actions]
            if len(buttons) != 1:
                raise NativeDriverError("无法唯一定位推荐牛人入口")
            await self.driver.perform(buttons[0], "press")
            await self.wait(is_candidate_list, "推荐牛人", allow_network_error=True)
        nodes = await self.snapshot()
        # Web dropdown labels include city/salary; the native top tabs only expose
        # the title. An explicitly different selector must not be silently ignored.
        title = selector_job_title or job_title
        matches = [n for n in nodes if n.role == "AXButton" and label(n) == title]
        if len(matches) != 1:
            raise NativeDriverError(f"客户端岗位未唯一匹配：{title!r}；请使用客户端显示的完整岗位名称")
        if matches[0].value != "选中":
            await self.driver.perform(matches[0], "press")
        await self.wait(
            lambda ns: is_candidate_list(ns) and any(
                n.role == "AXButton" and label(n) == title and n.value == "选中" for n in ns
            ), "岗位切换", stable_for=1.0,
        )
        await self.press_label("推荐")
        await self.wait(
            lambda ns: is_candidate_list(ns) and any(
                n.role == "AXButton" and label(n) == title and n.value == "选中" for n in ns
            ), "推荐候选人", stable_for=1.0,
        )
        logger.info("已选职位：%s", title)
        return await self.apply_history_filter(title)

    async def apply_history_filter(self, job_title):
        """Restore this job's saved filters, then enable the fourteen-day history option."""
        nodes = await self.snapshot()
        buttons = [n for n in nodes if n.role == "AXButton" and "AXPress" in n.actions
                   and re.fullmatch(r"筛选(?:·\d+)?", label(n))]
        if len(buttons) != 1:
            raise NativeDriverError("无法唯一定位推荐筛选入口")
        await self.driver.perform(buttons[0], "press")
        nodes = await self.wait(lambda ns: has_label(ns, "近期没有看过"), "推荐历史筛选面板")
        saved_prompt = "是否应用该职位上次的筛选条件？"
        if has_label(nodes, saved_prompt):
            applications = [n for n in nodes if n.role == "AXButton" and label(n) == "应用"
                            and "AXPress" in n.actions]
            if len(applications) != 1:
                raise NativeDriverError("上次筛选条件的应用按钮必须唯一")
            await self.driver.perform(applications[0], "press")
            nodes = await self.wait(
                lambda ns: has_label(ns, "近期没有看过") and not has_label(ns, saved_prompt),
                "加载当前职位上次筛选条件",
            )
            logger.info("已加载当前职位上次筛选条件：职位=%s", job_title)

        def count(ns):
            titles = [re.fullmatch(r"筛选(?:·(\d+))?", label(n)) for n in ns if n.role == "AXStaticText"]
            matches = [match for match in titles if match is not None]
            if len(matches) != 1:
                raise NativeDriverError("筛选计数标题必须唯一")
            return int(matches[0][1] or 0)

        def history_row(ns):
            rows = [n for n in ns if n.role == "AXStaticText" and label(n) == "不限, 近14天没有"
                    and "AXPress" in n.actions]
            if len(rows) != 1:
                raise NativeDriverError("近14天没有看过筛选行必须唯一")
            return rows[0]

        initial = count(nodes)
        await self.driver.perform(history_row(nodes), "press")
        nodes = await self.wait(lambda ns: count(ns) != initial, "历史筛选计数变化")
        changed = count(nodes)
        # UIKit 不暴露该行选中态。计数 +1 证明已开启；-1 表示刚关闭，
        # 须从新快照重取该行再开启，不重置或修改其他筛选条件。
        if changed == initial - 1:
            await self.driver.perform(history_row(nodes), "press")
            nodes = await self.wait(lambda ns: count(ns) != changed, "重新开启历史筛选")
            if count(nodes) != initial:
                raise NativeDriverError("重新开启历史筛选后计数异常，停止确认")
        elif changed != initial + 1:
            raise NativeDriverError("历史筛选计数变化异常，停止确认")
        confirms = [n for n in nodes if n.role == "AXButton" and label(n) == "确认" and "AXPress" in n.actions]
        if len(confirms) != 1:
            raise NativeDriverError("筛选确认按钮必须唯一")
        await self.driver.perform(confirms[0], "press")
        nodes = await self.wait(
            lambda ns: is_candidate_list(ns) and not has_label(ns, "近期没有看过") and any(
                n.role == "AXButton" and label(n) == job_title and n.value == "选中" for n in ns
            ), "历史筛选确认后的推荐列表", stable_for=1.0,
        )
        logger.info("历史筛选已开启：职位=%s，近14天没有看过", job_title)
        return nodes

    @staticmethod
    def candidate_cards(nodes):
        cards = []
        for node in nodes:
            if node.role != "AXStaticText" or "AXPress" not in node.actions:
                continue
            text = label(node)
            if not re.search(r"(?:年|应届).*?(?:初中|高中|中专|大专|本科|硕士|博士)", text):
                continue
            # The card's employer is an adjacent accessibility node.
            siblings = [label(n) for n in nodes if n.path[:-1] == node.path[:-1]
                        and n.role == "AXStaticText"]
            cards.append((node, "\n".join(siblings)))
        return cards

    async def read_resume(self):
        nodes = await self.wait(is_resume, "在线简历", stable_for=1.0)

        def metadata_ready(ns):
            return is_resume(ns) and any(has_label(ns, heading) for heading in ("求职期望", "最近关注")) and any(
                has_label(ns, section) for section in ("工作经历", "教育经历")
            )

        def page_signature(ns):
            return tuple((label(n), round(n.rect["y"], 1)) for n in ns if n.role == "AXStaticText")

        async def move_to_top(ns):
            for _ in range(80):
                scrolls = [n for n in ns if n.role == "AXGroup" and "AXScrollUpByPage" in n.actions]
                if not scrolls:
                    if not metadata_ready(ns):
                        ns = await self.wait(metadata_ready, "简历顶部资料")
                    return ns
                before = page_signature(ns)
                await self.driver.perform(scrolls[0], "scrollUp")
                ns = await self.wait(is_resume, "简历返回顶部", stable_for=0.6)
                # UIKit can retain AXScrollUpByPage even at the top boundary.
                if page_signature(ns) == before:
                    if not metadata_ready(ns):
                        raise NativeDriverError("向上滚动已停止，但未识别到简历顶部资料")
                    return ns
            raise NativeDriverError("简历向上滚动超过 80 页，未确认回到顶部")

        nodes = await move_to_top(nodes)
        chunks = []
        seen_chunks = set()
        for _ in range(80):
            # These section controls hide actual resume entries. Expand before
            # collecting the page, then resolve all nodes again from a fresh tree.
            for expansion in range(8):
                buttons = [n for n in nodes if "AXPress" in n.actions and
                           re.fullmatch(r"展开(?:全部)?(?:工作经历|项目经历|教育经历)", label(n))]
                if not buttons:
                    break
                await self.driver.perform(buttons[0], "press")
                nodes = await self.wait(is_resume, "展开完整简历经历", stable_for=0.6)
            else:
                raise NativeDriverError("简历经历展开后仍未消失，无法确认完整内容")

            scrolls = [n for n in nodes if n.role == "AXGroup" and
                       any(a in n.actions for a in ("AXScrollDownByPage", "AXScrollUpByPage", "AXScrollRightByPage"))]
            scroll_path = scrolls[0].path if scrolls else None
            by_path = {n.path: n for n in nodes}
            page_cells = {}
            for n in nodes:
                text = label(n)
                if n.role != "AXStaticText" or not text or text == "BOSS直聘" or text.startswith("“不合适、举报”功能"):
                    continue
                # Direct child groups are UIKit resume cells. Their paths are
                # recycled as we scroll, so use the whole cell (employer, dates,
                # title and responsibilities) to merge overlapping pages. Equal
                # job titles or duties in different employers remain intact.
                cell_path = None
                if scroll_path is not None and n.path[:len(scroll_path)] == scroll_path and len(n.path) > len(scroll_path) + 1:
                    candidate = n.path[:len(scroll_path) + 1]
                    if candidate in by_path and by_path[candidate].role == "AXGroup":
                        cell_path = candidate
                key = ("cell", cell_path) if cell_path is not None else ("node", n.path)
                page_cells.setdefault(key, []).append(text)
            occurrences = {}
            for (kind, path), texts in page_cells.items():
                content = tuple(texts)
                if kind == "cell":
                    ordinal = occurrences.get(content, 0)
                    occurrences[content] = ordinal + 1
                    identity = (kind, content, ordinal)
                else:
                    identity = (kind, path, content)
                if identity not in seen_chunks:
                    seen_chunks.add(identity)
                    chunks.append(content)
            scrolls = [n for n in nodes if n.role == "AXGroup" and "AXScrollDownByPage" in n.actions]
            if not scrolls:
                await move_to_top(nodes)
                return "\n".join(text for chunk in chunks for text in chunk)
            before = page_signature(nodes)
            await self.driver.perform(scrolls[0], "scrollDown")
            nodes = await self.wait(is_resume, "简历翻页", stable_for=0.6)
            if page_signature(nodes) == before:
                await move_to_top(nodes)
                return "\n".join(text for chunk in chunks for text in chunk)
        raise NativeDriverError("简历滚动超过 80 页，未确认完整读取")


def parse_native_resume(text):
    profile_boundary = re.search(r"(?m)^[ \t]*(?:求职期望|最近关注)[ \t]*$", text)
    parsed = job_utils.parse_resume(text[:profile_boundary.start()] if profile_boundary else text)
    # Salary belongs to the expectation section; historical prose is not a
    # substitute when a candidate has hidden their expected salary.
    expectation_heading = re.search(r"(?m)^[ \t]*求职期望[ \t]*$", text)
    expectation = text[expectation_heading.end():] if expectation_heading else ""
    expectation = re.split(r"(?m)^[ \t]*(?:最近关注|工作经历|教育经历)[ \t]*$", expectation, maxsplit=1)[0]
    expectation = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", expectation)
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*(万元|元|[kK])", expectation)
    parsed["salary_lower_bound"] = parsed["salary_upper_bound"] = None
    if match:
        scale = {"万元": 10000, "元": 1, "k": 1000}[match[3].lower()]
        parsed["salary_lower_bound"] = int(float(match[1]) * scale)
        parsed["salary_upper_bound"] = int(float(match[2]) * scale)
    elif "面议" in expectation:
        parsed["salary_lower_bound"] = parsed["salary_upper_bound"] = -1
    return parsed


def prefilter_resume(text, requirements):
    parsed = parse_native_resume(text)
    if parsed["age"] is None or not requirements["age_lower_bound"] <= parsed["age"] <= requirements["age_upper_bound"]:
        return "age"
    if requirements["gender"] is not None and parsed["gender"] != requirements["gender"]:
        return "gender"
    salary = parsed["salary_lower_bound"]
    if requirements["maximum_salary"] > 0 and not (salary is not None and 0 < salary < requirements["maximum_salary"]):
        return "salary"
    if parsed["education"] is None or parsed["education"] < requirements["education"]:
        return "education"
    if requirements["off_the_job"] > 0 and parsed["job_status"] != "离职-随时到岗":
        return "employment_status"
    if not job_utils.matches_keyword_tree(requirements["cv_required_keywords"], text):
        return "keywords"
    return "passed"


async def confirm_initial_greeting(session, chat_nodes):
    """Confirm the client's automatic first message without submitting another."""
    from native_greetings import chat_position

    _, expected_card = chat_position(chat_nodes)
    history = expected_card.path[:-2]
    if not history:
        raise NativeDriverError("无法定位首次沟通的消息区域")

    def delivered(nodes):
        _, card = chat_position(nodes)
        if card.description != expected_card.description:
            raise NativeDriverError("首次招呼确认期间候选人发生变化，停止处理")
        for status in nodes:
            if status.path[:len(history)] != history or label(status) not in ("送达", "已读"):
                continue
            siblings = [n for n in nodes if n.path[:-1] == status.path[:-1]]
            # The real client exposes the sender avatar, delivery status and
            # message text in the same outgoing message cell.
            if (any(n.role == "AXButton" and label(n) == "求职者头像" for n in siblings)
                    and any(n.role == "AXStaticText" and label(n)
                            and label(n) not in ("送达", "已读") for n in siblings)):
                return True
        return False

    await session.wait(delivered, "自动首次招呼送达确认（未确认时停止，不重复发送）")


async def loop_recommend(session, max_idx, requirements, client, stats, job_title, allow_contact=False):
    counters = stats.setdefault(job_title, {})
    for key in ("viewed", "qualified", "greeted", "scanned", "card_prefilter_skipped"):
        counters.setdefault(key, 0)
    seen = set()
    stale_opens = 0
    while counters["scanned"] < max_idx:
        nodes = await session.wait(is_candidate_list, "候选人列表")
        cards = session.candidate_cards(nodes)
        if not cards and not any(has_label(nodes, text) for text in ("暂无推荐牛人", "暂无合适牛人", "暂无推荐")):
            raise NativeDriverError("未识别到推荐候选人卡片，停止处理")
        candidate = next(((node, text) for node, text in cards if text not in seen), None)
        if candidate is None:
            scrolls = [n for n in nodes if n.role == "AXGroup" and "AXScrollDownByPage" in n.actions]
            if not scrolls:
                break
            before = tuple((text, round(n.rect["y"], 1)) for n, text in cards)
            await session.driver.perform(scrolls[0], "scrollDown")
            nodes = await session.wait(is_candidate_list, "候选人翻页", stable_for=1.0)
            if tuple((text, round(n.rect["y"], 1)) for n, text in session.candidate_cards(nodes)) == before:
                break
            continue
        node, card_text = candidate
        if "已看过" in card_text or "已沟通" in card_text:
            seen.add(card_text)
            counters["scanned"] += 1
            continue
        if not job_utils.matches_keyword_tree(requirements["cv_required_keywords"], card_text):
            seen.add(card_text)
            counters["scanned"] += 1
            counters["card_prefilter_skipped"] += 1
            job_utils.save_resume_diagnostic(
                job_title, counters["scanned"], job_utils.parse_resume(card_text),
                job_utils.get_matched_keywords(requirements["cv_required_keywords"], card_text),
                False, "card_keywords", card_text,
            )
            logger.info("推荐卡片关键词未通过，未打开详情：职位=%s #%s", job_title, counters["scanned"])
            continue
        try:
            await session.driver.perform(node, "press")
        except NativeStaleElementError:
            # A recommendation refresh can invalidate the snapshot before the
            # bridge presses anything. Re-resolve the list, without consuming a
            # candidate or retrying any contact/submission action.
            stale_opens += 1
            if stale_opens >= 3:
                raise
            logger.info("推荐列表已刷新，重新定位候选人：职位=%s", job_title)
            continue
        stale_opens = 0
        seen.add(card_text)
        counters["scanned"] += 1
        try:
            text = await session.read_resume()
        except TimeoutError:
            # Continue only after proving the detail page was actually closed.
            await session.return_to_candidates()
            logger.warning("简历加载超时，已返回列表，跳过当前候选人")
            continue
        counters["viewed"] += 1
        stage = prefilter_resume(text, requirements)
        parsed = parse_native_resume(text)
        job_utils.save_resume_diagnostic(
            job_title, counters["scanned"], parsed,
            job_utils.get_matched_keywords(requirements["cv_required_keywords"], text),
            stage == "passed", stage, text,
        )
        if stage == "passed":
            assessed_page = page_identity(await session.snapshot()) if allow_contact else None
            result = await asyncio.to_thread(
                llm_utils.is_qualified_text_result, client, text, requirements["cv_requirements"]
            )
            if result is not None and result.is_qualified:
                counters["qualified"] += 1
                if allow_contact:
                    current = await session.snapshot()
                    if not is_resume(current) or page_identity(current) != assessed_page:
                        raise NativeDriverError("评估期间简历页面或候选人发生变化，停止联系")
                    await session.press_label("立即沟通")
                    chat_nodes = await session.wait(lambda ns: has_label(ns, "求简历"), "沟通页面")
                    counters["contact_opened"] = counters.get("contact_opened", 0) + 1
                    await confirm_initial_greeting(session, chat_nodes)
                    counters["greeted"] += 1
                    logger.info("推荐候选人的自动首次招呼已确认送达")
                else:
                    logger.info("候选人符合要求；只读模式，未打招呼")
            elif result is None:
                logger.warning("LLM 评估失败，跳过当前候选人")
        else:
            logger.info("预筛未通过：%s", stage)
        await session.return_to_candidates()
    return counters["viewed"], counters["greeted"]


async def run_native(job_configs, client, stats, *, allow_contact=False,
                     greetings=True, recommend=True, driver=None, rejection_reviewer=None):
    from native_driver import NativeDriver
    from native_greetings import loop_greetings

    session = NativeSession(driver or NativeDriver())
    if greetings:
        review_options = {"rejection_reviewer": rejection_reviewer} if rejection_reviewer is not None else {}
        await loop_greetings(session, job_configs, client, stats, allow_contact=allow_contact, **review_options)
    if recommend:
        for params in job_configs:
            requirements = job_utils.get_job_requirements(params["job_requirements"])
            await session.select_job(params["job_title"], requirements["selector_job_title"])
            await loop_recommend(session, params.get("max_idx", 120), requirements,
                                 client, stats, params["job_title"], allow_contact)
