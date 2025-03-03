# from langchain.chat_models import ChatOpenAI
# from langchain.prompts import PromptTemplate
# from langchain.embeddings import OpenAIEmbeddings
from pydantic import BaseModel
import logging, os
from dotenv import load_dotenv
from log_utils import logger
load_dotenv()

# LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

system_message = """你是一位经验丰富的人力资源专家和面试官。你的任务是根据给定的职位要求，评估候选人的工作经历是否符合要求。请仔细分析候选人的经历与职位要求之间的匹配度，考虑技能、经验、行业背景等多个方面。在评估后，请给出明确的判断:

 - 如果候选人符合职位要求，请回复True
 - 如果候选人不符合职位要求，请回复False

请确保你的判断是客观、公正的，并基于所提供的信息。"""

prompt_template = """
        候选人简历:
        {}
        
        
        职位要求:
        {} 
        
        
        请根据以上信息，评估该候选人是否符合这个职位的要求。记住，你的回答应该只是True或False。
    """


class interviewer(BaseModel):
    is_qualified: bool

def is_qualified(client, resume_text, resume_requirement):
    if resume_requirement:
        response = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": system_message
                },
                {
                    "role": "user",
                    "content": prompt_template.format(resume_text, resume_requirement)
                },
                {
                    "role": "assistant",
                    "content": "0"
                }
            ],
            temperature=0.2,
            max_tokens=500,
            response_format = interviewer,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0
        )
        result = response.choices[0].message.parsed
        logger.info(f"{result.is_qualified} - {' '.join(resume_text[:50].splitlines())}")
        # if result_text == "1":
        #     return True
        # elif result_text == "0":
        #     return False
        # else:
        #     print(f"OPENAI 回复为：{result_text}")
        #     return False
        return result.is_qualified
