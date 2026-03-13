import os, argparse, asyncio, glob, subprocess
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
        parts = []
        if 'viewed' in stats:
            parts.append(f"简历查看数 {stats['viewed']}")
        if 'greeted' in stats:
            parts.append(f"打招呼人数 {stats['greeted']}")
        if 'requested' in stats:
            parts.append(f"求简历人数 {stats['requested']}")
        log_utils.logger.llm(f"职位 {job_title}：{'，'.join(parts)}")


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

def clear_chrome_locks():
    subprocess.run(["pkill", "-9", "-f", "chrome_dev_test"], capture_output=True)
    patterns = [
        '/tmp/chrome_dev_test/Singleton*',
        '/tmp/chrome_dev_test/Default/Lock',
        '/tmp/chrome_dev_test/Default/LOCK',
    ]
    for pattern in patterns:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

async def launch_browser(url):
    clear_chrome_locks()
    await asyncio.sleep(1)
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
    # Get all job configurations
    job_configs = get_params()

    # Launch browser once
    browser, tab = await launch_browser(job_configs[0]['url'])
    await driver_utils.log_in(tab)
    await driver_utils.close_popover(tab)

    try:
        # Process each job configuration with WakeLock to prevent system sleep
        with wakelock_utils.WakeLock():
            # Phase 1: inbound greeting candidates (新招呼)
            await driver_utils.goto_new_greetings(tab)
            await job_utils.loop_greetings(tab, job_configs, client, job_stats)
            await driver_utils.close_popover(tab)

            # Phase 2: outbound recommendation screening (推荐牛人)
            await driver_utils.goto_recommend(tab)
            for params in job_configs:
                job_title = params['job_title']
                max_idx = params.get('max_idx', 120)
                log_utils.logger.info(f"开始处理职位：{job_title}")

                # close popover
                await driver_utils.close_popover(tab)

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
    except TimeoutError as e:
        log_utils.logger.warning(f"服务器无响应，终止处理：{e}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log_final_stats()
        await browser.stop()


if __name__ == '__main__':
    asyncio.run(main())
