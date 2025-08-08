import os, argparse, time, atexit
import commentjson as json
from openai import OpenAI

import undetected_chromedriver as uc
# from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import driver_utils, llm_utils, job_utils, log_utils

global driver

# from packaging import version
from dotenv import load_dotenv

load_dotenv()


OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')

# Initialize OpenAI client
if OPENAI_BASE_URL:
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
else:
    client = OpenAI(api_key=OPENAI_API_KEY)


# Global variable to store job statistics
job_stats = {}

def log_final_stats():
    """Function to log final statistics when program exits"""
    log_utils.logger.llm("职位处理统计：")
    for job_title, stats in job_stats.items():
        log_utils.logger.llm(f"职位 {job_title}：简历查看数 {stats['viewed']}，打招呼人数 {stats['greeted']}")


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

    options = uc.ChromeOptions()

    # 禁用Web安全性以允许跨域Canvas操作
    options.add_argument('--disable-web-security')
    options.add_argument('--disable-features=VizDisplayCompositor')
    options.add_argument('--allow-running-insecure-content')
    options.add_argument('--disable-extensions')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--enable-network-service-logging')
    options.add_argument("--timeout=30")
    options.add_argument("--read-timeout=30")

    # 允许跨域访问
    options.add_argument('--user-data-dir=/tmp/chrome_dev_test')
    options.add_argument('--allow-cross-origin-auth-prompt')
    driver = uc.Chrome(use_subprocess=True, options=options)
    driver.get(url)
    # driver.maximize_window()
    time.sleep(2)
    return driver


if __name__ == '__main__':
    # Register the exit handler
    atexit.register(log_final_stats)

    # Get all job configurations
    job_configs = get_params()

    # Launch webdriver once
    driver = launch_webdriver(job_configs[0]['url'])
    driver_utils.log_in(driver)
    driver_utils.close_popover(driver)
    driver_utils.goto_recommend(driver)


    # Process each job configuration
    for params in job_configs:
        job_title = params['job_title']
        max_idx = params.get('max_idx', 120)
        log_utils.logger.info(f"开始处理职位：{job_title}")

        # close popover
        driver_utils.close_popover(driver)

        # Select specific job position
        driver_utils.select_job_position(driver, job_title)

        # Get job requirements
        job_requirements = job_utils.get_job_requirements(params['job_requirements'])

        # Scan recommend loop for this specific job
        viewed, greeted = job_utils.loop_recommend(driver, max_idx, job_requirements, client, job_stats, job_title)

        # 记录每个职位的统计信息
        job_stats[job_title] = {
            'viewed': viewed,
            'greeted': greeted
        }

    # Close driver after processing all jobs
    driver.quit()
