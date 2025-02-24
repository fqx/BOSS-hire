import time

# from selenium import webdriver
from selenium.common import NoSuchElementException, ElementClickInterceptedException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
import re

from io import BytesIO
from PIL import Image
import base64

import logging, os
from dotenv import load_dotenv
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')


# import undetected_chromedriver as uc
xpath_age = '//*[@id="recommend-list"]/div/ul/li[{}]/div/div[1]/div[2]/div[2]/div'
xpath_resume_card_is_viewed = '//*[@id="recommend-list"]/div/ul/li[{}]'
xpath_resume_card = '//*[@id="recommend-list"]/div/ul/li[{}]/div/div[1]'
xpath_resume_page = '//div[@class="resume-detail-wrap"]'
xpath_resume_section = '//div[starts-with(@class, "resume-section")]'
xpath_say_hi = '//button[@class="btn-v2 btn-sure-v2 btn-greet"]'
xpath_i_know_after_say_hi = '//button[contains(text(),"知道了")]'
xpath_resume_close = '//i[@class="icon-close"]'


def log_in(driver):
    driver.get('https://www.zhipin.com/web/user/?intent=1')
    time.sleep(3)
    qr_button = driver.find_element(By.XPATH, '//*[@id="wrap"]/div/div[2]/div[2]/div[1]')
    qr_button.click()
    # time.sleep(1)
    # imgdata = base64.b64decode(driver.get_screenshot_as_base64())
    # img = Image.open(BytesIO(imgdata))
    # logging.info('请扫描二维码登录。')
    # img.show()
    wait = WebDriverWait(driver, 30)
    wait.until(EC.url_to_be('https://www.zhipin.com/web/chat/index'))

    logging.info("Logged in.")
    time.sleep(3)


def goto_recommend(driver):
    driver.find_element(By.LINK_TEXT, "推荐牛人").click()
    iframe = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.NAME, "recommendFrame"))
    )
    time.sleep(5)
    driver.switch_to.frame(iframe)


def find_resume_card(driver, idx):
    div = driver.find_element(By.XPATH, xpath_resume_card.format(idx))
    return div


def is_viewed(driver, idx):
    try:
        card = driver.find_element(By.XPATH, xpath_resume_card_is_viewed.format(idx))
        card_inner =card.find_element(By.CSS_SELECTOR, "div[class^='card-inner']")
        is_viewed = "has-viewed" in card_inner.get_attribute("class")
        return is_viewed
    except NoSuchElementException:
        logging.warning(f"#{idx} 加载失败。")
        scroll_down(driver)
        time.sleep(3)
        return False


def get_age(driver, idx):
    while True:
        try:
            age = driver.find_element(By.XPATH, xpath_age.format(idx)).text
            break
        except:
            logging.warning('载入更多简历')
            # driver.execute_script("window.scrollTo(0, window.scrollY + 180)")
            time.sleep(1)
            return 99
    age = int(re.findall(r'\d+', age)[0])
    return age


def get_resume(driver, div):
    time.sleep(3)
    # div.click()
    # ActionChains(driver).move_to_element(div).click().perform()
    driver.execute_script("arguments[0].click();", div)
    wait = WebDriverWait(driver, 10)
    wait.until(EC.visibility_of_element_located((By.XPATH, xpath_resume_page)))
    # resume_detail = driver.find_element(By.XPATH, xpath_resume_page)
    resume_text = []
    elements = driver.find_elements(By.XPATH, xpath_resume_section)
    for element in elements:
        text = element.get_attribute('textContent').strip()  # Remove leading/trailing whitespace
        if text:  # Only add non-empty strings
            resume_text.append(text)

    resume_text = " ■ ".join(resume_text)  # Join with a custom separator
    # resume_text =  resume_detail.get_attribute('textContent').strip()
    resume_text = re.sub('\\n\s+','',resume_text)
    return resume_text


def say_hi(driver):
    time.sleep(2)
    say_hi_botton = driver.find_element(By.XPATH, xpath_say_hi)
    say_hi_botton.click()
    time.sleep(1)
    try:
        driver.find_element(By.XPATH, xpath_i_know_after_say_hi).click()
    except:
        pass


def close_resume(driver):
    time.sleep(2)
    driver.find_element(By.XPATH, xpath_resume_close).click()


def scroll_down(driver):
    time.sleep(0.5)
    driver.execute_script("window.scrollTo(0, window.scrollY + 180)")


def select_job_position(driver, job_title):
    """
    Method to search and select specific job position on BOSS website

    Args:
        driver: Selenium WebDriver instance
        job_title: Job title to search and select
    """
    try:
        # Click on the dropdown menu to expand job options
        dropdown_menu = driver.find_element(By.XPATH, "//div[@class='ui-dropmenu-label']")
        dropdown_menu.click()
        time.sleep(1)  # Wait for dropdown to expand

        # Find all job options in the dropdown
        job_options = driver.find_elements(By.XPATH, "//div[@class='ui-dropmenu-list']/ul/li")

        # Find the first job option that starts with the given job_title
        selected_job = None
        for option in job_options:
            if option.text.startswith(job_title):
                selected_job = option
                break

        # If a matching job is found, click on it
        if selected_job:
            selected_job.click()
            logging.info(f"Selected job: {job_title}")
            time.sleep(5)  # Wait for page to load after selection
        else:
            logging.warning(f"No job found starting with: {job_title}")

    except Exception as e:
        logging.error(f"Error selecting job position: {e}")
        raise
