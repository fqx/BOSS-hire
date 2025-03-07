import os, argparse, time, json

from openai import OpenAI

import undetected_chromedriver as uc
import driver_utils, llm_utils, job_utils, log_utils

global driver

from packaging import version
from dotenv import load_dotenv

load_dotenv()


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
        config = json.load(open(args.config))
        # If not a list, convert to list for consistent processing
        return [config] if not isinstance(config, list) else config
    else:
        config = json.load(open("params.json"))
        # If not a list, convert to list for consistent processing
        return [config] if not isinstance(config, list) else config

def launch_webdriver(url):
    driver = uc.Chrome(use_subprocess=True)
    driver.get(url)
    # driver.maximize_window()
    time.sleep(2)
    return driver


if __name__ == '__main__':
    # Get all job configurations
    job_configs = get_params()

    # Launch webdriver once
    driver = launch_webdriver(job_configs[0]['url'])
    driver_utils.log_in(driver)

    driver_utils.goto_recommend(driver)

    # 用于存储每个职位的统计信息
    job_stats = {}

    # Process each job configuration
    for params in job_configs:
        job_title = params['job_title']
        max_idx = params.get('max_idx', 120)
        log_utils.logger.info(f"开始处理职位：{job_title}")


        # Select specific job position
        driver_utils.select_job_position(driver, job_title)

        # Get job requirements
        job_requirements = job_utils.get_job_requirements(params['job_requirements'])

        # Scan recommend loop for this specific job
        viewed, greeted = job_utils.loop_recommend(driver, max_idx, job_requirements, client)

        # 记录每个职位的统计信息
        job_stats[job_title] = {
            'viewed': viewed,
            'greeted': greeted
        }

    # Close driver after processing all jobs
    driver.quit()

  # 最终输出每个职位的状态
    log_utils.logger.info("职位处理统计：")
    for job_title, stats in job_stats.items():
        log_utils.logger.info(f"职位 {job_title}：简历查看数 {stats['viewed']}，打招呼人数 {stats['greeted']}")
