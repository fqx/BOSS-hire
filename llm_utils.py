# from langchain.chat_models import ChatOpenAI
# from langchain.prompts import PromptTemplate
# from langchain.embeddings import OpenAIEmbeddings
import json
import re
import time
from datetime import date
from pydantic import BaseModel
from requests.exceptions import Timeout
import os
import openai
from dotenv import load_dotenv
from log_utils import logger
load_dotenv()

# LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5-mini")

system_message = """## Role and Goal
You are a highly experienced, meticulous, and objective Human Resources (HR) evaluation expert. Your primary mission is to act as an automated screening agent, rigorously assessing a candidate's resume against a given job description (JD). Your final output must be a precise, rule-based determination of the candidate's qualification status.

## Core Evaluation Principles
1.  **Objectivity and Evidence-Based:** Your entire analysis must be based *solely* on the text provided in the candidate's resume and the job description. Do not fabricate facts or apply speculative external knowledge. However, you **must** perform reasonable contextual inference from evidence present in the resume text itself: if a candidate's job responsibilities, products worked on, employer descriptions, or domain-specific terminology clearly indicate a particular industry, you **must** treat this as valid industry experience — even if the candidate does not use the exact phrase from the JD. For example, a candidate who worked on medical device firmware is inferred to have medical device industry experience; a candidate whose resume describes work on loan disbursement systems at a bank is inferred to have financial industry experience.
2.  **Strict Adherence to Rules:** You must follow all specified rules, especially the distinction between "must-have" and "preferred" conditions, the salary calculation, and the output formatting. There is no room for leniency or "almost-fits" judgments.
3.  **Consistency is Paramount:** The final boolean flag `is_qualified` must perfectly mirror the conclusion stated in the first sentence of the `reason`. This is a critical consistency check.

## Detailed Step-by-Step Instructions for Evaluation
Follow this sequence precisely for every candidate evaluation. Do not deviate.

### Step 1: Initial Salary Check (Hard Rule)
This is the first and most critical gate.
- **Identify Candidate's Minimum Salary:**
    - If a range is given (e.g., 10k-15k), use the lower bound (10k).
    - If a single value is given, use that value.
    - If it's "面议" (negotiable), "保密" (confidential), or not provided, the value is considered "Unknown".
- **Identify Job's Minimum Salary:**
    - From the job's salary range (e.g., 5k-6k), use the lower bound (5k).
    - If not provided, the value is "Unknown".
- **Apply the Rule:** The candidate is **disqualified** if `Candidate's Minimum Salary > (Job's Minimum Salary * 1.5)`.
    - **Example:** If the job is 10k-15k, the minimum is 10k. The maximum acceptable candidate minimum is `10k * 1.5 = 15k`. A candidate asking for a minimum of 16k is disqualified.
- **Unknown Information Rule:** If the candidate's minimum salary is "Unknown" and the job's minimum salary is not "Unknown", the check **fails**. The candidate is disqualified.
- **Proceed or Stop:** If the salary check fails, stop all further analysis. The candidate is not qualified. If it passes, proceed to Step 2.

### Step 2: Sequential "Must-Have" Condition Check
If the salary check was successful, now evaluate the mandatory requirements one by one, in the order they appear in the JD.
- **Identifying "Must-Have" Conditions:** A condition is considered mandatory if it contains keywords like "需要" (need), "必须" (must), "硬性" (hard requirement), "要求" (require), "至少" (at least), "不低于" (no less than), "限定" (limited to), "仅限" (only for), "必备" (essential). Also, treat explicit, non-negotiable quantifiers (e.g., "学历：本科", "3年经验", "持有PMP证书", "工作地点在北京") as "must-have" conditions.
- **Identifying "Preferred" Conditions:** A condition is non-mandatory (a plus) if it contains keywords like "优先" (preferred), "加分" (bonus points), "最好" (best if), "希望" (hope), "熟悉" (familiar with), "了解" (understand), "优先考虑" (will be considered with priority), "可选" (optional), "不限" (no limit). If a JD says "学历不限" (education unlimited), it cannot be used as a disqualifying factor.
- **Industry Experience Inference:** When a JD requires experience in a specific industry (e.g., "医疗行业经验", "金融行业背景", "新能源领域"), do NOT require the candidate to state this explicitly. Instead, look for supporting evidence in the resume: the nature of the company (from the candidate's own description), the products or services the candidate worked on, the domain terminology used in job duties, or the sector of the candidate's clients. If such evidence clearly points to the required industry, the condition is met. Only reject on this criterion if there is no such evidence at all.
- **Stop at First Failure:** As soon as you find the *first* "must-have" condition that the candidate does not meet, stop all further analysis. The candidate is disqualified.

### Step 3: Special Case Handling
- **Employment Status:** If the candidate's current employment status listed at the top of the resume (e.g., "离职-随时到岗") conflicts with the dates of their last job entry, you **must** trust the status listed at the top.
- **Online Activity:** Do not interpret a candidate's online activity status (e.g., "在线", "刚刚活跃", "x日内活跃") as their employment status. It is irrelevant to the evaluation.

如果候选人不符合，还需要在 reason_category 字段中输出一个最匹配的原因，
必须从以下选项中选择一个（原文，不能修改）：
薪资不符、学历不符、年龄不符、期望不符、距离太远、过往经历不符、简历不真实、已找到工作、其他原因
如果候选人符合，reason_category 输出空字符串 ""。

## Output Generation Rules (Strictly Enforced)
Your final output must be a JSON object containing `is_qualified`, `reason`, and `reason_category`. The generation of this output must follow a specific, unchangeable order.

1.  **Determine the Final Conclusion:** Based on your step-by-step analysis, you will have a final conclusion: either "Qualified" or "Not Qualified" (with the specific reason for failure).

2.  **Write the `reason` First Sentence:** This sentence is formulaic and depends entirely on the conclusion from Step 1.
    - If the conclusion is "Qualified", the sentence **must** be: `候选人{姓名}符合该职位`
    - If the conclusion is "Not Qualified", the sentence **must** be: `候选人{姓名}不符合该职位`
    - If the candidate's name cannot be identified, use "候选人" as a substitute.

3.  **Set the `is_qualified` Boolean:** Now, set the boolean value to match the `reason`'s first sentence perfectly.
    - If the first sentence starts with "符合该职位" -> `is_qualified = True`.
    - If the first sentence starts with "不符合该职位" -> `is_qualified = False`.

4.  **Complete the `reason` Body:**
    - **For "Qualified" cases:** After the required first sentence, add a brief summary of why the candidate is a good match, highlighting key qualifications.
    - **For "Not Qualified" cases:** After the required first sentence, you **must immediately** state the *very first* rule that the candidate failed. Provide a brief, evidence-based explanation using data from the JD and resume. For example, "...因为该职位要求候选人目前处于离职状态，而候选人简历显示其仍在职。" or "...因为候选人期望的最低薪酬高于职位最低薪酬的1.5倍。"
    - **Writing Style:** The `reason` must be direct and conclusive. Avoid transitional words like "但是" (but), "然而" (however), or conditional statements like "如果..." (if...).

### Final Self-Correction Check
Before producing the final output, perform a mandatory self-check: Does the value of `is_qualified` perfectly align with the verdict in the first sentence of `reason`? If not, you must correct `is_qualified` to match the `reason`'s statement. This is a non-negotiable final step. Do not suggest negotiation, flexibility, or alternative outcomes.
"""

class interviewer(BaseModel):
    reason: str
    is_qualified: bool
    reason_category: str = ""  # one of 9 preset values, or "" if qualified


def _parse_content(content: str) -> interviewer:
    """Parse raw text into interviewer when output_parsed is unavailable."""
    # Strategy 1: direct JSON parse
    try:
        return interviewer(**json.loads(content))
    except Exception:
        pass
    # Strategy 2: JSON inside markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?})\s*```", content, re.DOTALL)
    if m:
        try:
            return interviewer(**json.loads(m.group(1)))
        except Exception:
            pass
    # Strategy 3: first JSON object in text
    m = re.search(r"\{.*}", content, re.DOTALL)
    if m:
        try:
            return interviewer(**json.loads(m.group(0)))
        except Exception:
            pass
    raise ValueError(f"Cannot parse LLM response: {content[:200]}")


PROMPT_CACHE_KEY = "hr_eval_prompt_v0105"
RETRY_DELAYS = [10, 30]  # seconds to wait before 1st and 2nd retry

_base_url = os.getenv("OPENAI_BASE_URL", "")
_local_hosts = ("localhost", "127.0.0.1", "::1")
_is_openai_cloud = (
    not any(h in _base_url for h in _local_hosts)
    and LLM_MODEL.lower().startswith("gpt")
)
MAX_OUTPUT_TOKENS = 1400  # for Responses API (cloud)
# Chat Completions API: thinking tokens are hidden, visible output is small JSON
MAX_TOKENS_CHAT = 2800


def _build_user_text(resume_requirement: str, overview_text: str) -> str:
    text = f"职位要求:\n{resume_requirement}\n\n"
    if overview_text:
        text += f"候选人经历概览（结构化工作、项目、教育经历摘要，供参考）:\n{overview_text}\n\n"
    return text


def _call_responses_api(client, resume_image_base64: str, resume_requirement: str, overview_text: str) -> interviewer:
    """OpenAI cloud path: Responses API with prompt caching and reasoning."""
    response = client.responses.parse(
        model=LLM_MODEL,
        prompt_cache_key=PROMPT_CACHE_KEY,
        instructions=system_message,
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _build_user_text(resume_requirement, overview_text)},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{resume_image_base64}"}
                ]
            }
        ],
        reasoning={"effort": "low"},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text_format=interviewer,
        timeout=60.0,
    )
    if response.output_parsed is not None:
        return response.output_parsed
    return _parse_content(response.output_text)


def _call_chat_api(client, resume_image_base64: str, resume_requirement: str, overview_text: str) -> interviewer:
    """Local LM Studio path: Chat Completions API with json_schema output."""
    today = date.today().strftime("%Y-%m-%d")
    system_with_date = f"Today's date is {today}.\n\n{system_message}"
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_with_date},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_user_text(resume_requirement, overview_text)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{resume_image_base64}"}}
                ]
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "interviewer", "schema": interviewer.model_json_schema()}
        },
        max_tokens=MAX_TOKENS_CHAT,
        timeout=120.0,
    )
    msg = response.choices[0].message
    content = msg.content or ""
    if not content.strip():
        # LM Studio 0.4.7+: with json_schema format, the structured output
        # is placed in reasoning_content while content is left empty.
        content = (msg.model_extra or {}).get("reasoning_content", "") or ""
    return _parse_content(content)


def _is_retryable(exc: Exception) -> bool:
    """Channel Error / server crash is worth retrying; bad requests are not."""
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    if isinstance(exc, openai.InternalServerError):
        return True
    # LM Studio reports model-backend crashes as APIStatusError with "Channel Error"
    if isinstance(exc, openai.APIStatusError) and "channel error" in str(exc).lower():
        return True
    return False


def _call_llm(client, resume_image_base64: str, resume_requirement: str, overview_text: str) -> interviewer | None:
    """Shared retry logic for both is_qualified and is_qualified_result."""
    for attempt, delay in enumerate([0] + RETRY_DELAYS, start=1):
        if delay:
            logger.warning(f"Retrying LLM request (attempt {attempt}) after {delay}s...")
            time.sleep(delay)
        try:
            if _is_openai_cloud:
                result = _call_responses_api(client, resume_image_base64, resume_requirement, overview_text)
            else:
                result = _call_chat_api(client, resume_image_base64, resume_requirement, overview_text)
            logger.llm(f"{result.is_qualified} - {result.reason:50}")
            return result
        except Timeout:
            logger.warning("LLM API request timed out")
            return None
        except Exception as e:
            if _is_retryable(e) and attempt <= len(RETRY_DELAYS):
                logger.warning(f"LLM backend error (will retry): {e}")
                continue
            logger.error(f"Error in LLM API request: {e}")
            return None
    return None


def is_qualified(client, resume_image_base64, resume_requirement, overview_text: str = ""):
    if not resume_requirement:
        return False
    result = _call_llm(client, resume_image_base64, resume_requirement, overview_text)
    return result.is_qualified if result is not None else False


def is_qualified_result(client, resume_image_base64, resume_requirement, overview_text: str = "") -> interviewer | None:
    """Return the full interviewer object (includes reason_category). Returns None on failure."""
    if not resume_requirement:
        return None
    return _call_llm(client, resume_image_base64, resume_requirement, overview_text)
