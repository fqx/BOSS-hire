import os, argparse, asyncio, glob, subprocess
import commentjson as json
import httpx
from timing_utils import jitter
from openai import OpenAI

import zendriver as zd
import driver_utils, llm_utils, job_utils, log_utils, wakelock_utils

# from packaging import version
from dotenv import load_dotenv

load_dotenv()


OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')

ENABLE_GREETINGS_LOOP = os.getenv('DISABLE_GREETINGS_LOOP', 'false').lower() != 'true'
ENABLE_RECOMMEND_LOOP = os.getenv('DISABLE_RECOMMEND_LOOP', 'false').lower() != 'true'

# Global variable to store job statistics
job_stats = {}

def log_final_stats():
    """Function to log final statistics when program exits"""
    log_utils.logger.llm("职位处理统计：")
    for job_title, stats in job_stats.items():
        parts = []
        if 'viewed' in stats:
            parts.append(f"简历查看数 {stats['viewed']}")
        if 'card_prefilter_skipped' in stats:
            parts.append(f"卡片关键词初筛跳过数 {stats['card_prefilter_skipped']}")
        if 'card_field_skipped' in stats:
            parts.append(f"卡片字段初筛跳过数 {stats['card_field_skipped']}")
        if 'greeted' in stats:
            parts.append(f"打招呼人数 {stats['greeted']}")
        if 'requested' in stats:
            parts.append(f"求简历人数 {stats['requested']}")
        if 'qualified' in stats:
            parts.append(f"符合人数 {stats['qualified']}")
        if 'inbound_viewed' in stats:
            parts.append(f"新招呼查看数 {stats['inbound_viewed']}")
        if 'inbound_qualified' in stats:
            parts.append(f"新招呼符合人数 {stats['inbound_qualified']}")
        if 'contact_opened' in stats:
            parts.append(f"打开沟通数 {stats['contact_opened']}")
        if 'marked_unsuitable' in stats:
            parts.append(f"标记不合适数 {stats['marked_unsuitable']}")
        if 'awaiting_review' in stats:
            parts.append(f"待人工复核数 {stats['awaiting_review']}")
        log_utils.logger.llm(f"职位 {job_title}：{'，'.join(parts)}")
        for pending in stats.get("pending_rejections", []):
            log_utils.logger.llm(
                f"待复核淘汰结论：职位 {job_title}\n原因分类：{pending['reason_category']}\n{pending['reason']}"
            )


def get_params(args=None):
    if args is None:
        args = get_arguments()
    config_path = args.config or "params.json"
    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    return [config] if not isinstance(config, list) else config


def create_openai_client():
    """Keep local model traffic off the system HTTP proxy."""
    base_url = OPENAI_BASE_URL or None
    options = {}
    if llm_utils.is_local_base_url(base_url):
        options["http_client"] = httpx.Client(trust_env=False)
    return OpenAI(api_key=OPENAI_API_KEY, base_url=base_url, timeout=60.0, **options)


def get_arguments():
    parser = argparse.ArgumentParser(description='根据职位要求筛选简历')
    parser.add_argument('-c', dest='config')
    parser.add_argument('--backend', choices=('native', 'web'), default='native',
                        help='默认操作 Mac 上的 iOS BOSS 客户端；web 保留浏览器流程')
    contact = parser.add_mutually_exclusive_group()
    contact.add_argument('--allow-contact', dest='allow_contact', action='store_true',
                         help='允许客户端打招呼、回复新招呼并按模型结论标记不合适（默认开启）')
    contact.add_argument('--read-only', dest='allow_contact', action='store_false',
                         help='客户端只筛选，不打招呼、回复或标记不合适')
    parser.set_defaults(allow_contact=True)
    parser.add_argument('--review-rejections', action='store_true',
                        help='逐人显示淘汰理由并确认；默认按模型结论自动标记不合适')
    args = parser.parse_args()
    if args.backend == 'web' and not args.allow_contact:
        parser.error('--read-only 当前仅支持 native 后端')
    if args.review_rejections and (args.backend != 'native' or not args.allow_contact):
        parser.error('--review-rejections 需要 native 后端且不能与 --read-only 同用')
    return args


async def review_rejection(job_title, result):
    """Require an explicit per-candidate decision before a native rejection."""
    # 控制台保留建议摘要，完整理由写入日志；逐人明确确认后才允许标记。
    log_utils.logger.llm(
        f"淘汰结论待人工复核：职位 {job_title}\n原因分类：{result.reason_category}\n{result.reason}"
    )
    try:
        answer = await asyncio.to_thread(input, "按此结论标记该候选人不合适？[y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")

def clear_chrome_locks():
    subprocess.run(["pkill", "-9", "-f", "chrome_user_data"], capture_output=True)
    patterns = [
        '/tmp/chrome_user_data/Singleton*',
        '/tmp/chrome_user_data/Default/Lock',
        '/tmp/chrome_user_data/Default/LOCK',
    ]
    for pattern in patterns:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

async def launch_browser(url):
    clear_chrome_locks()
    await asyncio.sleep(jitter(1))
    browser = await zd.start(
        headless=False,
        user_data_dir='/tmp/chrome_user_data',
        browser_args=[
            '--disable-dev-shm-usage',
            '--disable-notifications',
            '--window-size=1920,1080',
        ],
        browser_connection_timeout=1.0,
        browser_connection_max_tries=15,
    )
    tab = await browser.get(url)
    await asyncio.sleep(jitter(2))
    return browser, tab


async def main():
    # Get all job configurations
    args = get_arguments()
    job_configs = get_params(args)
    if not job_configs:
        raise ValueError("职位配置不能为空")
    client = create_openai_client()

    if args.backend == 'native':
        from native_workflow import run_native
        from native_driver import NativeDailyLimitError
        try:
            with wakelock_utils.WakeLock():
                # 默认允许正常联系并按模型结论标记；显式开启复核才逐人询问。
                review_options = {"rejection_reviewer": review_rejection} if args.review_rejections else {}
                await run_native(
                    job_configs, client, job_stats,
                    allow_contact=args.allow_contact,
                    greetings=ENABLE_GREETINGS_LOOP,
                    recommend=ENABLE_RECOMMEND_LOOP,
                    **review_options,
                )
        except NativeDailyLimitError as error:
            log_utils.logger.warning("达到客户端每日查看/沟通上限，停止处理：%s", error)
        finally:
            log_final_stats()
        return

    # Launch browser once
    browser, tab = await launch_browser(job_configs[0]['url'])
    try:
        await driver_utils.log_in(tab)
        await driver_utils.close_popover(tab)
        # Process each job configuration with WakeLock to prevent system sleep
        with wakelock_utils.WakeLock():
            # Phase 1: inbound greeting candidates (新招呼)
            if ENABLE_GREETINGS_LOOP:
                greeting_count = await driver_utils.goto_new_greetings(tab)
                if greeting_count > 0:
                    await job_utils.loop_greetings(tab, job_configs, client, job_stats, total=greeting_count)
                else:
                    log_utils.logger.info("新招呼为空，跳过。")
                await driver_utils.close_popover(tab)
            else:
                log_utils.logger.info("新招呼处理已禁用（DISABLE_GREETINGS_LOOP=true）。")

            # Phase 2: outbound recommendation screening (推荐牛人)
            if not ENABLE_RECOMMEND_LOOP:
                log_utils.logger.info("推荐牛人处理已禁用（DISABLE_RECOMMEND_LOOP=true）。")
                return
            await driver_utils.goto_recommend(tab)
            for params in job_configs:
                job_title = params['job_title']
                max_idx = params.get('max_idx', 120)
                log_utils.logger.info(f"开始处理职位：{job_title}")

                # close popover
                await driver_utils.close_popover(tab)

                # Resolve screening and selector requirements independently.
                job_requirements = job_utils.get_job_requirements(params['job_requirements'])

                # Select specific job position
                await driver_utils.select_job_position(
                    tab,
                    job_title,
                    job_requirements['selector_job_title'],
                )

                # Scan recommend loop for this specific job
                viewed, greeted = await job_utils.loop_recommend(tab, max_idx, job_requirements, client, job_stats, job_title)

                # 记录每个职位的统计信息
                job_stats[job_title] = {
                    'viewed': viewed,
                    'greeted': greeted
                }
    except driver_utils.CaptchaRequired:
        log_utils.logger.error(
            "检测到滑块验证页面，程序已暂停。请在浏览器中完成验证，完成后按 Enter 键退出，重新运行程序即可继续。"
        )
        try:
            await asyncio.to_thread(input)
        except (EOFError, KeyboardInterrupt):
            pass
    except driver_utils.PlatformAccessError as e:
        log_utils.logger.error(f"账号访问不可用，终止处理：{e}")
    except TimeoutError as e:
        log_utils.logger.warning(f"服务器无响应，终止处理：{e}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log_final_stats()
        await browser.stop()


if __name__ == '__main__':
    asyncio.run(main())
