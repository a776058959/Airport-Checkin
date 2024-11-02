import requests
import json
import os
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from lxml import etree
import re
import subprocess
import time

requests.packages.urllib3.disable_warnings()
SCKEY = os.environ.get('SCKEY')
TG_BOT_TOKEN = os.environ.get('TGBOT')
TG_USER_ID = os.environ.get('TGUSERID')

def sky():
        chrome_options = Options()
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument("--headless")
        chrome_options.add_argument('--ignore-certificate-errors') 
        chrome_options.add_argument('--ignore-ssl-errors')
        driver = webdriver.Chrome(options=chrome_options)
        capabilities = DesiredCapabilities.CHROME.copy()
        capabilities['acceptInsecureCerts'] = True
        try:
            # 找到滑块按钮元素
            driver.get(base_url)

            slider = driver.find_element(By.ID, "handler")
            # 使用ActionChains来模拟滑动
            actions = ActionChains(driver)
            actions.click_and_hold(slider)  # 按住滑块
            actions.move_by_offset(322, 0)  # 向右滑动，可以根据需要调整滑动距离
            actions.release()  # 释放滑块
            actions.perform()  # 执行滑动操作
            time.sleep(5)
            logi()
        except NameError:
            print("名字异常")
        except :
            print("NoSuchElementException异常")
            logi()
            return
        
def logi():
    lurl = base_url + '/auth/login'
    driver.get(lurl)
    time.sleep(5)
    print("------输入邮箱")
    username_input = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "email")))
    username_input.send_keys(email)
    time.sleep(1)
    print("------输入密码")
    password_input = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "password")))
    password_input.send_keys(password)
    time.sleep(1)
    print("------登录")
    driver.find_element(By.ID, "login_submit").click()

    lurl = base_url + '/user'
    time.sleep(1)
    driver.get(lurl)
    print("------准备签到")
    try:
        time.sleep(5)
        # 尝试读取已签到标签
        try:
            driver.find_element(By.XPATH, '//*[@id="kt_subheader"]/div/div[2]/a').text
            time.sleep(1)
            print("------已签到")
            ll()
            return
        except :
            try:
                 driver.find_element(By.ID, "checkin").click()
            except:
                print("------签到报错")
                driver.get_screenshot_as_file('screenshot0.png')
                driver.save_screenshot("screenshot1.png")
                print("------查看出错网页截图")
            else:
                print("------签到成功")
                ll()
    finally:
        print("完成")
        time.sleep(5)
        driver.quit()

def ll():
    syll = driver.find_element(By.XPATH, '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[1]/div/div/div/strong').text
    yyll = driver.find_element(By.XPATH, '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[2]/p').text
    print("剩余流量:" + syll)
    print(yyll)

def checkin(email=os.environ.get('EMAIL'), password=os.environ.get('PASSWORD'),
            base_url=os.environ.get('BASE_URL'), ):
    if "fawncloud" in base_url:
        return sky()
    else:
        print("------")

    email = email.split('@')
    email = email[0] + '%40' + email[1]
    session = requests.session()
    session.get(base_url, verify=False)
    login_url = base_url + '/auth/login'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/56.0.2924.87 Safari/537.36',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    }
    post_data = 'email=' + email + '&passwd=' + password + '&code='
    post_data = post_data.encode()
    response = session.post(login_url, post_data, headers=headers, verify=False)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/56.0.2924.87 Safari/537.36',
        'Referer': base_url + '/user'
    }
    response = session.post(base_url + '/user/checkin', headers=headers,
                            verify=False)
    try:
        response = json.loads(response.text)
    except json.decoder.JSONDecodeError:
        print("JSONDecodeError: No valid JSON object could be decoded from the string.")
        print(response.text)
    print(response['msg'])
    return response['msg']


result = checkin()
if SCKEY != '':
    sendurl = 'https://sctapi.ftqq.com/' + SCKEY + '.send?title=机场签到&desp=' + result
    r = requests.get(url=sendurl)
if TG_USER_ID != '':
    sendurl = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage?chat_id={TG_USER_ID}&text={result}&disable_web_page_preview=True'
    r = requests.get(url=sendurl)