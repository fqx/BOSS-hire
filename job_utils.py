from selenium.common import ElementClickInterceptedException
from tqdm import tqdm
import driver_utils, llm_utils
import os
from log_utils import logger
from dotenv import load_dotenv
load_dotenv()


default_job_requirements = {
        'age_lower_bound': 0,
        'age_upper_bound': 100,
        'cv_required_keywords': [],
        'cv_requirements': '',
    }


def get_job_requirements(job_requirements):
    for key, value in job_requirements.items():
        default_job_requirements[key] = value
    return default_job_requirements


def check_if_contains_any_character(a_list, b_string):
  """
  Checks if a string contains any character from a list of strings.

  Args:
    a_list (list): The list of strings to check.
    b_string (str): The string to check for characters.

  Returns:
    bool: True if b_string contains any character from a_list, False otherwise.
  """

  # If the list is empty, return True.
  if not a_list:
    return True

  # Iterate over the list of strings and check if b_string contains any of them.
  for string in a_list:
    if string in b_string:
      return True

  # If no match is found, return False.
  return False


def loop_recommend(driver, max_idx, job_requirements, client, job_stats, job_title):
    idx = 0
    viewed = 0
    greeted = 0
    def update_job_stats(job_title, viewed = 0, greeted = 0):
        job_stats[job_title] = {
            'viewed': viewed,
            'greeted': greeted
        }
    update_job_stats(job_title, viewed, greeted)

    # 获取日志处理器并设置当前tqdm实例
    log_handler = logger.handlers[0]

    # Wrap the main loop with tqdm
    with tqdm(total=max_idx, desc=f"Processing Resumes for {job_title}", unit="resume",
              leave=True) as pbar:
        # 将当前进度条实例传递给日志处理器
        log_handler.set_tqdm(pbar)

        while idx < max_idx:
            try:
                idx += 1
                if driver_utils.is_viewed(driver, idx):
                    logger.info(f"#{idx} 已经查看过。")
                    driver_utils.scroll_down(driver)
                    pbar.update(1)
                    # pbar.refresh()
                    continue

                age = driver_utils.get_age(driver, idx)
                if job_requirements['age_lower_bound'] <= age <= job_requirements['age_upper_bound']:
                    div_resume = driver_utils.find_resume_card(driver, idx)
                    if check_if_contains_any_character(job_requirements['cv_required_keywords'],
                                                       div_resume.get_attribute('textContent')):
                        logger.info("#{} 年龄符合要求，并且含有关键字。调用LLM进一步处理。".format(idx))

                        resume_image_base64 = driver_utils.get_resume(driver, div_resume)
                        is_qualified = llm_utils.is_qualified(client, resume_image_base64, job_requirements['cv_requirements'])
                        viewed += 1
                        update_job_stats(job_title, viewed, greeted)

                        if is_qualified:
                            logger.info(f"#{idx} 符合要求，打招呼。")
                            driver_utils.say_hi(driver)
                            greeted += 1
                            update_job_stats(job_title, viewed, greeted)
                        else:
                            logger.info(f"#{idx} 不符合要求。")
                        driver_utils.close_resume(driver)
                        driver_utils.scroll_down(driver)
                        pbar.update(1)
                        # pbar.refresh()
                        continue

                logger.info('#{} 不符合要求。'.format(idx))
                driver_utils.scroll_down(driver)
                pbar.update(1)
                # pbar.refresh()

            except ElementClickInterceptedException as e:
                logger.warning(f"An error occurred: {e}")
                logger.info("Try next one.")
                pbar.update(1)
                # pbar.refresh()
                continue

            except Exception as e:
                logger.warning(f"An error occurred: {e}")
                break

    log_handler.set_tqdm(None)
    logger.info(f"简历查看数：{viewed}   打招呼人数：{greeted}")
    return viewed, greeted
