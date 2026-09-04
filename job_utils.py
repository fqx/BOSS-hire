from tqdm import tqdm
import asyncio
import driver_utils, llm_utils
import json
import os, re
from log_utils import logger
from dotenv import load_dotenv
load_dotenv()

def parse_resume(resume_text):
    """
    Parses a single resume snippet and extracts structured information.

    Args:
        resume_text: A string containing a single resume snippet.

    Returns:
        A dictionary containing the extracted information.
    """
    education_map = {
        "初中及以下": 1,
        "高中": 2,
        "中专/中技": 3,
        "大专": 4,
        "本科": 5,
        "硕士": 6,
        "博士": 7,
    }

    # Default values
    parsed_data = {
        "salary_lower_bound": None,
        "salary_upper_bound": None,
        "name": None,
        "age": None,
        "education": None,
        "job_status": None,
    }

    # Salary
    salary_match = re.search(r"(\d+)-(\d+)K", resume_text)
    if salary_match:
        parsed_data["salary_lower_bound"] = int(salary_match.group(1)) * 1000
        parsed_data["salary_upper_bound"] = int(salary_match.group(2)) * 1000
    elif "面议" in resume_text:
        parsed_data["salary_lower_bound"] = -1  # Using -1 to represent negotiable
        parsed_data["salary_upper_bound"] = -1

    # Name
    name_match = re.search(r"K\s+([^\s]+)\s+", resume_text)
    if not name_match:
        name_match = re.search(r"面议\s+([^\s]+)\s+", resume_text)
    if name_match:
        parsed_data["name"] = name_match.group(1)

    # Age
    age_match = re.search(r"(\d+)岁", resume_text)
    if age_match:
        parsed_data["age"] = int(age_match.group(1))

    # Education
    education_match = re.search(r"(初中及以下|高中|中专/中技|大专|本科|硕士|博士)", resume_text)
    if education_match:
        parsed_data["education"] = education_map.get(education_match.group(1))

    # On-the-job status
    status_match = re.search(r"(离职-随时到岗|在职-月内到岗|\d{2}年毕业|\d{2}年应届生|应届生)", resume_text)
    if status_match:
        parsed_data["job_status"] = status_match.group(1)
    else:
        education_end_index = -1
        education_match_for_position = re.search(r"(初中及以下|高中|中专/中技|大专|本科|硕士|博士)", resume_text)
        if education_match_for_position:
            education_end_index = education_match_for_position.end()

        expectation_start_index = resume_text.find("期望：")

        if education_end_index != -1 and expectation_start_index != -1:
            status_text = resume_text[education_end_index:expectation_start_index].strip()
            parsed_data["job_status"] = status_text
        else:
            # A final fallback for cases where even the positional logic fails
            status_match = re.search(r"\d+岁\d+年(.*?)(期望|优势)", resume_text)
            if status_match:
                parsed_data["job_status"] = status_match.group(1).strip()

    return parsed_data

default_job_requirements = {
    'age_lower_bound': 0,
    'age_upper_bound': 100,
    "maximum_salary": -1,
    "selector_job_title": None,
    "education": 0,
    "off_the_job": 0,
    'cv_required_keywords': [],
    'cv_requirements': '',
}


def get_job_requirements(job_requirements):
    result = dict(default_job_requirements)
    result.update(job_requirements)
    return result


def get_matched_keywords(keywords, resume_text):
    """Return configured keywords found in a resume card, preserving config order."""
    return [keyword for keyword in keywords if keyword in resume_text]


def resume_diagnostics_enabled():
    return os.getenv("SAVE_RESUME_DIAGNOSTICS", "false").lower() == "true"


def save_resume_diagnostic(
    job_title,
    idx,
    resume_dict,
    matched_keywords,
    prefilter_passed,
    elimination_stage,
    resume_card_text,
):
    """Optionally append local resume-card diagnostics for recommendation tuning."""
    if not resume_diagnostics_enabled():
        return False

    configured_output_path = os.getenv("RESUME_DIAGNOSTICS_PATH")
    output_path = configured_output_path or os.path.join(
        "diagnostics", "resume_cards.jsonl"
    )
    output_dir = os.path.dirname(output_path)
    record = {
        "job_title": job_title,
        "idx": idx,
        "age": resume_dict.get("age"),
        "salary_lower_bound": resume_dict.get("salary_lower_bound"),
        "salary_upper_bound": resume_dict.get("salary_upper_bound"),
        "education": resume_dict.get("education"),
        "job_status": resume_dict.get("job_status"),
        "matched_keywords": matched_keywords,
        "prefilter_passed": prefilter_passed,
        "elimination_stage": elimination_stage,
        "resume_card_text": resume_card_text,
    }
    encoded_record = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        if output_dir:
            os.makedirs(output_dir, mode=0o700, exist_ok=True)
            # Resume cards contain personal information. Tighten even an existing
            # diagnostics directory before opening the output file; if this fails,
            # the exception is caught below and no candidate data is written.
            os.chmod(output_dir, 0o700)
        else:
            raise PermissionError(
                "RESUME_DIAGNOSTICS_PATH must use a dedicated parent directory"
            )
        file_descriptor = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.fchmod(file_descriptor, 0o600)
            written = os.write(file_descriptor, encoded_record)
            if written != len(encoded_record):
                raise OSError(
                    f"partial diagnostic append: wrote {written}/{len(encoded_record)} bytes"
                )
        finally:
            os.close(file_descriptor)
    except OSError as error:
        logger.warning(
            "简历诊断写入失败，招聘流程继续：path=%s error=%s",
            output_path,
            error,
        )
        return False

    logger.info(
        "简历诊断：职位=%s #%s 阶段=%s 预筛=%s 命中关键词=%s",
        job_title,
        idx,
        elimination_stage,
        prefilter_passed,
        matched_keywords,
    )
    return True


async def save_age_rejection_diagnostic(tab, job_title, idx, age, keywords):
    """Capture an age-stage record when diagnostics are enabled and the card is readable."""
    if not resume_diagnostics_enabled():
        return
    try:
        resume_text = await driver_utils.get_resume_card_text(tab, idx)
        if not resume_text:
            save_resume_diagnostic(
                job_title, idx, {"age": age}, [], False, "parse_error", ""
            )
            return
        resume_dict = parse_resume(resume_text)
        if resume_dict['age'] is None:
            resume_dict['age'] = age
        save_resume_diagnostic(
            job_title,
            idx,
            resume_dict,
            get_matched_keywords(keywords, resume_text),
            False,
            "age",
            resume_text,
        )
    except Exception as error:
        logger.warning("#%s 年龄淘汰诊断解析失败：%s", idx, error)
        save_resume_diagnostic(
            job_title, idx, {"age": age}, [], False, "parse_error", ""
        )


async def loop_greetings(tab, job_configs: list, client, job_stats: dict, total: int = 0) -> int:
    """Process unread candidates in 新招呼 for all configured job positions."""
    job_map = {
        cfg['job_title']: get_job_requirements(cfg.get('job_requirements', {}))
        for cfg in job_configs
    }

    MAX_SCAN = 200
    idx = 1
    processed = 0
    skipped = 0
    already_navigated = False

    log_handler = logger.handlers[0]
    with tqdm(desc="新招呼", unit="人", total=total or None) as pbar:
        log_handler.set_tqdm(pbar)
        try:
            while idx <= MAX_SCAN:
                await driver_utils.raise_if_captcha(tab, "during greeting scan")
                matched = None
                if not already_navigated:
                    unread = await driver_utils.is_greeting_unread(tab, idx)
                    if unread is None:
                        break
                    if not unread:
                        idx += 1
                        continue

                    list_title = await driver_utils.get_greeting_job_title_at(tab, idx)
                    matched = next((t for t in job_map if list_title.startswith(t)), None)
                    if matched is None:
                        logger.info(f"跳过（职位未配置）：{list_title}")
                        idx += 1
                        skipped += 1
                        continue

                    await driver_utils.open_greeting_at(tab, idx)
                already_navigated = False

                chat_title = await driver_utils.get_current_chat_job_title(tab)
                if not chat_title:
                    break
                matched = next((t for t in job_map if chat_title.startswith(t)), matched)

                if matched is None:
                    logger.info(f"跳过（职位未配置，自动导航）：{chat_title!r}")
                    idx += 1
                    skipped += 1
                    continue

                requirements = job_map[matched]

                try:
                    await driver_utils.open_online_resume_greeting(tab)
                    canvas_b64, overview_text = await asyncio.wait_for(
                        driver_utils.get_online_resume_greeting(tab),
                        timeout=driver_utils.GREETING_RESUME_LOAD_TIMEOUT,
                    )
                except (TimeoutError, Exception) as e:
                    logger.warning(f"在线简历加载失败，跳过：{e}")
                    idx += 1
                    pbar.update(1)
                    continue

                result = llm_utils.is_qualified_result(
                    client, canvas_b64, requirements['cv_requirements'], overview_text
                )
                await driver_utils.close_online_resume_greeting(tab)

                if result is None:
                    logger.warning("LLM 调用失败，跳过")
                    idx += 1
                    pbar.update(1)
                    continue

                try:
                    if result.is_qualified:
                        logger.info(f"符合，发送招呼消息：{matched}")
                        await driver_utils.request_resume(tab)
                        # already_navigated intentionally NOT set here: request_resume (send message)
                        # does not trigger platform auto-navigation. Setting it True would cause an
                        # infinite loop if the platform stays on the same candidate (re-process same
                        # person indefinitely). Worst case without the flag: one candidate skipped
                        # if the platform ever does auto-navigate after a send — an acceptable loss.
                        job_stats.setdefault(matched, {})['requested'] = job_stats.get(matched, {}).get('requested', 0) + 1
                    else:
                        reason = result.reason_category or '其他原因'
                        logger.info(f"不符合（{reason}），标记不合适：{matched}")
                        await driver_utils.mark_unsuitable(tab, reason)
                        # mark_unsuitable removes the candidate from the list and the platform
                        # auto-opens the next candidate, clearing their unread badge. Skip the
                        # unread check on the next iteration to avoid skipping that candidate.
                        already_navigated = True
                except Exception as e:
                    logger.warning(f"执行操作失败，跳过：{e}")
                    idx += 1
                    pbar.update(1)
                    continue

                processed += 1
                pbar.update(1)
                # After action, this item leaves 新招呼; idx stays (next candidate fills slot)
        finally:
            log_handler.set_tqdm(None)

    logger.info(f"新招呼完成：处理 {processed} 人，跳过（职位未配置）{skipped} 人")
    return processed


async def loop_recommend(tab, max_idx, job_requirements, client, job_stats, job_title):
    idx = 0
    viewed = 0
    greeted = 0
    def update_job_stats(job_title, viewed = 0, greeted = 0):
        job_stats[job_title] = {
            'viewed': viewed,
            'greeted': greeted
        }
    update_job_stats(job_title, viewed, greeted)

    # Create file for saving resume texts
    # os.makedirs('resume_texts', exist_ok=True)
    # resume_text_file = os.path.join('resume_texts', f"{job_title.replace('/', '_')}_resumes.txt")

    # 获取日志处理器并设置当前tqdm实例
    log_handler = logger.handlers[0]

    # Wrap the main loop with tqdm
    with tqdm(total=max_idx, desc=f"Processing Resumes for {job_title}", unit="resume",
              leave=True) as pbar:
        # 将当前进度条实例传递给日志处理器
        log_handler.set_tqdm(pbar)

        while idx < max_idx:
            try:
                await driver_utils.raise_if_captcha(tab, "during recommendation scan")
                idx += 1
                if await driver_utils.is_viewed(tab, idx):
                    logger.info(f"#{idx} 已经查看过。")
                    await driver_utils.scroll_down(tab)
                    pbar.update(1)
                    continue

                age = await driver_utils.get_age(tab, idx)
                if job_requirements['age_lower_bound'] <= age <= job_requirements['age_upper_bound']:
                    resume_text = await driver_utils.get_resume_card_text(tab, idx)

                    try:
                        if not resume_text:
                            raise ValueError("empty resume card text")
                        resume_dict = parse_resume(resume_text)
                    except Exception as error:
                        logger.warning(f"#{idx} 简历卡片解析失败：{error}")
                        save_resume_diagnostic(
                            job_title, idx, {"age": age}, [], False,
                            "parse_error", resume_text or "",
                        )
                        await driver_utils.scroll_down(tab)
                        pbar.update(1)
                        continue
                    if resume_dict['age'] is None:
                        resume_dict['age'] = age
                    matched_keywords = get_matched_keywords(
                        job_requirements['cv_required_keywords'], resume_text
                    )
                    if job_requirements['maximum_salary'] <= 0 or (resume_dict['salary_lower_bound'] is not None and job_requirements['maximum_salary'] > resume_dict['salary_lower_bound'] > 0):
                        # salary ok
                        if resume_dict['education'] >= job_requirements['education']: # education ok
                            if job_requirements['off_the_job'] <= 0 or resume_dict['job_status'] == '离职-随时到岗': # off_the_job ok:

                                if not job_requirements['cv_required_keywords'] or matched_keywords:
                                    save_resume_diagnostic(
                                        job_title, idx, resume_dict, matched_keywords, True,
                                        "passed", resume_text,
                                    )
                                    logger.info("#{} 简历符合要求。调用LLM进一步处理。".format(idx))

                                    resume_image_base64, overview_text = await asyncio.wait_for(
                                        driver_utils.get_resume(tab, idx),
                                        timeout=driver_utils.RESUME_LOAD_TIMEOUT,
                                    )
                                    is_qualified = llm_utils.is_qualified(client, resume_image_base64, job_requirements['cv_requirements'], overview_text)
                                    viewed += 1
                                    update_job_stats(job_title, viewed, greeted)

                                    if is_qualified:
                                        logger.info(f"#{idx} 符合要求，打招呼。")
                                        try:
                                            await driver_utils.say_hi(tab)
                                        except driver_utils.DailyGreetingLimitReached:
                                            logger.warning(f"当前职位今日打招呼已达上限，停止处理：{job_title}")
                                            await driver_utils.close_resume(tab)
                                            break
                                        greeted += 1
                                        update_job_stats(job_title, viewed, greeted)
                                    else:
                                        logger.info(f"#{idx} 不符合要求。")
                                    await driver_utils.close_resume(tab)
                                    await driver_utils.scroll_down(tab)
                                    pbar.update(1)
                                    continue
                                else:
                                    save_resume_diagnostic(
                                        job_title, idx, resume_dict, matched_keywords, False,
                                        "keywords", resume_text,
                                    )
                                    logger.info('#{} 关键词不符合要求。'.format(idx))
                                    await driver_utils.scroll_down(tab)
                                    pbar.update(1)
                                    continue
                            else:
                                save_resume_diagnostic(
                                    job_title, idx, resume_dict, matched_keywords, False,
                                    "employment_status", resume_text,
                                )
                                logger.info('#{} 在职情况不符合要求。'.format(idx))
                                await driver_utils.scroll_down(tab)
                                pbar.update(1)
                                continue
                        else:
                            save_resume_diagnostic(
                                job_title, idx, resume_dict, matched_keywords, False,
                                "education", resume_text,
                            )
                            logger.info('#{} 教育情况不符合要求。'.format(idx))
                            await driver_utils.scroll_down(tab)
                            pbar.update(1)
                            continue
                    else:
                        save_resume_diagnostic(
                            job_title, idx, resume_dict, matched_keywords, False,
                            "salary", resume_text,
                        )
                        logger.info('#{} 薪资不符合要求。'.format(idx))
                        await driver_utils.scroll_down(tab)
                        pbar.update(1)
                        continue

                await save_age_rejection_diagnostic(
                    tab,
                    job_title,
                    idx,
                    age,
                    job_requirements['cv_required_keywords'],
                )
                logger.info('#{} 年龄不符合要求。'.format(idx))
                await driver_utils.scroll_down(tab)
                pbar.update(1)
                continue

            except TimeoutError:
                logger.warning(f"#{idx} 简历加载超时 ({driver_utils.RESUME_LOAD_TIMEOUT}s)，终止处理。")
                raise
            except Exception as e:
                logger.warning(f"An error occurred: {e}")
                logger.info("Try next one.")
                pbar.update(1)
                continue

    log_handler.set_tqdm(None)
    logger.info(f"简历查看数：{viewed}   打招呼人数：{greeted}")
    return viewed, greeted
