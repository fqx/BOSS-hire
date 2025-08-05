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

system_message = """你是一位经验丰富的人力资源专家和面试官。你的任务是根据给定的职位要求，评估候选人的工作经历是否符合要求。请仔细分析候选人的经历与职位要求之间的匹配度，考虑技能、经验、行业背景等多个方面。你也需要关注候选人简历期望职位部分，候选人是否愿意从事职位工作，候选人期望的薪酬不应高于职位的150%。在评估后，请给出明确的判断:

 - 如果候选人符合职位要求，请回复True
 - 如果候选人不符合职位要求，请回复False

请确保你的判断是客观、公正的，并基于所提供的信息。"""


class interviewer(BaseModel):
    is_qualified: bool
    reason: str


def is_qualified(client, resume_image_base64, resume_requirement):
    if resume_requirement:
        try:
            response = client.beta.chat.completions.parse(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"职位要求:\n{resume_requirement}\n\n请根据以上职位要求和下面的简历图片，评估该候选人是否符合这个职位的要求，并简要说明原因。原因要以“候选人张三”(将张三替换为候选人姓名）开头。"
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
                temperature=0.2,
                max_tokens=500,
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
