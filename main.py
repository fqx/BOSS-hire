import os, argparse, asyncio, atexit
import commentjson as json
from openai import OpenAI

import zendriver as zd
import driver_utils, llm_utils, job_utils, log_utils, wakelock_utils

# from packaging import version
from dotenv import load_dotenv

load_dotenv()


OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')

# Initialize OpenAI client with timeout
if OPENAI_BASE_URL:
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, timeout=60.0)
else:
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)


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

async def launch_browser(url):
    browser = await zd.start(
        headless=False,
        user_data_dir='/tmp/chrome_dev_test',
        browser_args=[
            '--disable-web-security',
            '--disable-features=VizDisplayCompositor',
            '--allow-running-insecure-content',
            '--disable-dev-shm-usage',
            '--disable-notifications',
            '--allow-cross-origin-auth-prompt',
        ]
    )
    tab = await browser.get(url)
    await asyncio.sleep(2)
    return browser, tab


async def main():
    # Register the exit handler
    atexit.register(log_final_stats)

    # Get all job configurations
    job_configs = get_params()

    # Launch browser once
    browser, tab = await launch_browser(job_configs[0]['url'])
    await driver_utils.log_in(tab)
    await driver_utils.close_popover(tab)
    await driver_utils.goto_recommend(tab)

    # Process each job configuration with WakeLock to prevent system sleep
    with wakelock_utils.WakeLock():
        for params in job_configs:
            job_title = params['job_title']
            max_idx = params.get('max_idx', 120)
            log_utils.logger.info(f"开始处理职位：{job_title}")

            # close popover
            # await driver_utils.close_popover(tab)

            # Select specific job position
            await driver_utils.select_job_position(tab, job_title)

            # Get job requirements
            job_requirements = job_utils.get_job_requirements(params['job_requirements'])

            # Scan recommend loop for this specific job
            viewed, greeted = await job_utils.loop_recommend(tab, max_idx, job_requirements, client, job_stats, job_title)

            # 记录每个职位的统计信息
            job_stats[job_title] = {
                'viewed': viewed,
                'greeted': greeted
            }

    # Close browser after processing all jobs
    await browser.stop()


if __name__ == '__main__':
    asyncio.run(main())
