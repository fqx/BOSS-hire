import driver_utils, llm_utils
import logging, os
from dotenv import load_dotenv
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

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


def loop_recommend(driver, max_idx, job_requirements, client):
    driver_utils.goto_recommend(driver)
    idx = 0
    while idx < max_idx:
        try:
            idx += 1
            if driver_utils.is_viewed(driver, idx):
                logging.info(f"#{idx} 已经查看过。")
                driver_utils.scroll_down(driver)
                continue
            age = driver_utils.get_age(driver, idx)
            if job_requirements['age_lower_bound'] <= age <= job_requirements['age_upper_bound']:
                div_resume = driver_utils.find_resume_card(driver, idx)
                if check_if_contains_any_character(job_requirements['cv_required_keywords'],
                                                             div_resume.get_attribute('textContent')):
                    # 年龄符合要求，并且含有关键字。调用LLM进一步处理
                    logging.info("#{} 年龄符合要求，并且含有关键字。调用LLM进一步处理。".format(idx))
                    resume_text = driver_utils.get_resume(driver, div_resume)
                    is_qualified = llm_utils.is_qualified(client, resume_text, job_requirements['cv_requirements'])
                    if is_qualified:
                        logging.info(f"#{idx} 符合要求，打招呼。")
                        driver_utils.say_hi(driver)
                    else:
                        logging.info(f"#{idx} 不符合要求。")
                    driver_utils.close_resume(driver)
                    driver_utils.scroll_down(driver)
                    continue

            logging.info('#{} 不符合要求。'.format(idx))
            driver_utils.scroll_down(driver)



        except Exception as e:
            logging.warning(f"An error occurred: {e}")
            break