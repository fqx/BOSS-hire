import os, argparse, time, json

from openai import OpenAI

import undetected_chromedriver as uc
import driver_utils, llm_utils, job_utils

global driver

from packaging import version
from dotenv import load_dotenv
import logging
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')

# Initialize OpenAI client
if OPENAI_BASE_URL:
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
else:
    client = OpenAI(api_key=OPENAI_API_KEY)


def get_params():
    parser = argparse.ArgumentParser(description='根据职位要求筛选简历')
    parser.add_argument('-c', dest='config')
    args = parser.parse_args()
    if args.config:
        return json.load(open(args.config))
    else:
        return json.load(open("params.json"))
def launch_webdriver(url):
    driver = uc.Chrome(use_subprocess=True)
    driver.get(url)
    # driver.maximize_window()
    time.sleep(2)
    return driver


if __name__ == '__main__':
    params = get_params()
    logging.info(f"开始处理职位：{params['job_title']}")

    driver = launch_webdriver(params['url'])
    driver_utils.log_in(driver)

    job_requirements = job_utils.get_job_requirements(params['job_requirements'])

    # scan recommend loop
    job_utils.loop_recommend(driver, 120, job_requirements, client)

    # scan new niuren loop

    driver.quit()
