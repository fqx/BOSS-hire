from selenium.common import ElementClickInterceptedException
from tqdm import tqdm
import driver_utils, llm_utils
import os, re
from log_utils import logger
from dotenv import load_dotenv
load_dotenv()

def parse_resume(resume_text):
    """
    Parses a single resume snippet and extracts structured information.

    Args:
        resume_text: A string containing a single resume snippet.

    Returns:
        A dictionary containing the extracted information.
    """
    education_map = {
        "初中及以下": 1,
        "高中": 2,
        "中专/中技": 3,
        "大专": 4,
        "本科": 5,
        "硕士": 6,
        "博士": 7,
    }

    # Default values
    parsed_data = {
        "salary_lower_bound": None,
        "salary_upper_bound": None,
        "name": None,
        "age": None,
        "education": None,
        "job_status": None,
    }

    # Salary
    salary_match = re.search(r"(\d+)-(\d+)K", resume_text)
    if salary_match:
        parsed_data["salary_lower_bound"] = int(salary_match.group(1)) * 1000
        parsed_data["salary_upper_bound"] = int(salary_match.group(2)) * 1000
    elif "面议" in resume_text:
        parsed_data["salary_lower_bound"] = -1  # Using -1 to represent negotiable
        parsed_data["salary_upper_bound"] = -1

    # Name
    name_match = re.search(r"K\s+([^\s]+)\s+", resume_text)
    if not name_match:
        name_match = re.search(r"面议\s+([^\s]+)\s+", resume_text)
    if name_match:
        parsed_data["name"] = name_match.group(1)

    # Age
    age_match = re.search(r"(\d+)岁", resume_text)
    if age_match:
        parsed_data["age"] = int(age_match.group(1))

    # Education
    education_match = re.search(r"(初中及以下|高中|中专/中技|大专|本科|硕士|博士)", resume_text)
    if education_match:
        parsed_data["education"] = education_map.get(education_match.group(1))

    # On-the-job status
    status_match = re.search(r"(离职-随时到岗|在职-月内到岗|\d{2}年毕业|\d{2}年应届生|应届生)", resume_text)
    if status_match:
        parsed_data["job_status"] = status_match.group(1)
    else:
        education_end_index = -1
        education_match_for_position = re.search(r"(初中及以下|高中|中专/中技|大专|本科|硕士|博士)", resume_text)
        if education_match_for_position:
            education_end_index = education_match_for_position.end()

        expectation_start_index = resume_text.find("期望：")

        if education_end_index != -1 and expectation_start_index != -1:
            status_text = resume_text[education_end_index:expectation_start_index].strip()
            parsed_data["job_status"] = status_text
        else:
            # A final fallback for cases where even the positional logic fails
            status_match = re.search(r"\d+岁\d+年(.*?)(期望|优势)", resume_text)
            if status_match:
                parsed_data["job_status"] = status_match.group(1).strip()

    return parsed_data

default_job_requirements = {
        'age_lower_bound': 0,
        'age_upper_bound': 100,
        "maximum_salary": -1,
        "education": 0,
        "off_the_job": 0,
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

    # Create file for saving resume texts
    # os.makedirs('resume_texts', exist_ok=True)
    # resume_text_file = os.path.join('resume_texts', f"{job_title.replace('/', '_')}_resumes.txt")

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
                    resume_text = div_resume.get_attribute('textContent')

                    # Save resume text to file
                    # with open(resume_text_file, 'a', encoding='utf-8') as f:
                    #     f.write(resume_text)
                    #     f.write('\n')

                    resume_dict = parse_resume(resume_text)
                    if job_requirements['maximum_salary'] <= 0 or (resume_dict['salary_lower_bound'] is not None and job_requirements['maximum_salary'] > resume_dict['salary_lower_bound'] > 0):
                        # salary ok
                        if resume_dict['education'] >= job_requirements['education']: # education ok
                            if job_requirements['off_the_job'] <= 0 or resume_dict['job_status'] == '离职-随时到岗': # off_the_job ok:

                                if check_if_contains_any_character(job_requirements['cv_required_keywords'], resume_text):
                                    logger.info("#{} 简历符合要求。调用LLM进一步处理。".format(idx))

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
                                else:
                                    logger.info('#{} 关键词不符合要求。'.format(idx))
                                    driver_utils.scroll_down(driver)
                                    pbar.update(1)
                                    continue
                            else:
                                logger.info('#{} 在职情况不符合要求。'.format(idx))
                                driver_utils.scroll_down(driver)
                                pbar.update(1)
                                continue
                        else:
                            logger.info('#{} 教育情况不符合要求。'.format(idx))
                            driver_utils.scroll_down(driver)
                            pbar.update(1)
                            continue
                    else:
                        logger.info('#{} 薪资不符合要求。'.format(idx))
                        driver_utils.scroll_down(driver)
                        pbar.update(1)
                        continue

                logger.info('#{} 年龄不符合要求。'.format(idx))
                driver_utils.scroll_down(driver)
                pbar.update(1)
                # pbar.refresh()
                continue

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
