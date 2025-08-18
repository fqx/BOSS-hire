# from langchain.chat_models import ChatOpenAI
# from langchain.prompts import PromptTemplate
# from langchain.embeddings import OpenAIEmbeddings
from pydantic import BaseModel
from requests.exceptions import Timeout
import logging, os
from dotenv import load_dotenv
from log_utils import logger
load_dotenv()

# LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

system_message = """你是一位经验丰富的人力资源专家和面试官。你的任务是根据给定的职位要求，评估候选人的工作经历是否符合要求。
请仔细分析候选人的经历与职位要求之间的匹配度，考虑技能、经验、行业背景等多个方面。
你还需要关注候选人简历期望职位部分，候选人是否愿意从事职位工作，候选人期望的最低薪酬不得高于该职位最低薪酬的1.5倍（即150%）。

必须条件的识别
视为必须：包含“需要/必须/硬性/要求/至少/不低于/限定/仅限/必备”等字样，或明确量化限制（学历/年限/证书/技能/行业/地域/在岗状态/到岗时间等）。
视为非必须：包含“优先/加分/最好/希望/熟悉/了解/优先考虑/可选/不限”等字样。
若职位写“不限”则该项不构成限制。
若候选人简历顶部显示的在岗状态与最后一份工作期间的状态不一致，以顶部显示的在岗状态为准。

薪酬硬性规则
候选人期望的最低薪酬 ≤ 职位“最低薪酬”的1.5倍。
候选人最低薪酬：其期望区间下限；若为单值则取该值；“面议/保密/未填”视为未知。
职位最低薪酬：职位给出的薪资下限/起薪/最低值；若未提供则视为未知。

输出一致性与生成顺序（必须遵守）：
1) 先依据本任务规则得到最终结论（先判薪酬硬性规则，再逐条判定“必须条件”；任一不满足即为不通过）。
2) 按该结论先写 reason 的第一句，固定句式二选一：
   - 若通过：以“候选人{姓名}符合该职位”开头，然后简述理由；
   - 若不通过：以“候选人{姓名}不符合该职位”开头，然后点明其首个未满足的“必须条件”。
3) 再设置 is_qualified，使其与 reason 第一句完全一致：
   - 若第一句以“符合该职位”开头 => is_qualified = True；
   - 若第一句以“不符合该职位”开头 => is_qualified = False。

reason 写作规范（必须遵守）：
- 第一句只给出最终结论，不得包含“但是/然而/不过”等转折词，也不得出现“如果…则…”等条件式或模糊表述。
- 若不通过，第二句起必须点明首个未满足的“必须条件”或“薪酬硬性规则”及简短证据（来自JD或简历）。
- 若无法识别姓名，用“候选人”代替。

冲突处理与自检（必须执行）：
- 返回前自检：若 is_qualified 与 reason 第一句不一致，必须以 reason 第一句为准，重设 is_qualified。
- 任一侧薪酬信息未知时，按规则视为不满足：reason 第一句应为“不符合该职位”，并设 is_qualified = False。
- 禁止提出协商、放宽或多方案答案。
"""


class interviewer(BaseModel):
    reason: str
    is_qualified: bool


def is_qualified(client, resume_image_base64, resume_requirement):
    if resume_requirement:
        try:
            response = client.beta.chat.completions.parse(
                model="gpt-5-mini",
                messages=[
                    {
                        "role": "developer",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"职位要求:\n{resume_requirement}\n\n"
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{resume_image_base64}"
                                }
                            }
                        ]
                    }
                ],
                # temperature=0.2,
                reasoning_effort="minimal",
                max_tokens=800,
                response_format = interviewer,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
                timeout=10.0  # 设置超时
            )
            result = response.choices[0].message.parsed
            logger.llm(f"{result.is_qualified} - {result.reason:50}")
            return result.is_qualified
        except Timeout:
            logger.warning("OpenAI API request timed out")
            return False
        except Exception as e:
            logger.error(f"Error in OpenAI API request: {e}")
            return False
