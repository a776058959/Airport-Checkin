import os
import time
import json
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 禁用requests的SSL警告
requests.packages.urllib3.disable_warnings()

class CheckinManager:
    def __init__(self):
        # 从环境变量获取基础配置（必需）
        self.email = os.environ.get('EMAIL')
        self.password = os.environ.get('PASSWORD')
        self.base_url = os.environ.get('BASE_URL')
        
        # 验证必需环境变量是否存在
        if not all([self.email, self.password, self.base_url]):
            raise ValueError("Missing required environment variables: EMAIL, PASSWORD, BASE_URL")
        
        # 从环境变量获取通知配置（可选）
        self.sckey = os.environ.get('SCKEY', '')
        self.tg_bot_token = os.environ.get('TGBOT', '')
        self.tg_user_id = os.environ.get('TGUSERID', '')
        
        # 初始化Selenium配置
        self.driver = self._init_selenium()
        
    def _init_selenium(self):
        """初始化Selenium WebDriver"""
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless")  # 无头模式
        chrome_options.add_argument('--no-sandbox')
        #chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-ssl-errors')
        return webdriver.Chrome(options=chrome_options)

    def handle_slider(self):
        """处理滑块验证（专用于fawncloud）"""
        try:
            print("正在检测滑块验证...")
            self.driver.get(self.base_url)
            # 显式等待滑块元素加载
            slider = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.ID, "handler"))
            )
            print("检测到滑块，开始模拟滑动...")
            
            # 创建动作链执行滑动操作
            actions = ActionChains(self.driver)
            actions.click_and_hold(slider)
            actions.move_by_offset(322, 0)  # 保持原有滑动距离
            actions.release().perform()
            print("滑块验证完成")
            time.sleep(2)  # 等待页面跳转
        except Exception as e:
            print(f"滑块处理异常（可能无需验证）: {str(e)}")

    def selenium_login(self):
        """Selenium登录流程"""
        try:
            print("正在执行Selenium登录...")
            self.driver.get(f"{self.base_url}/auth/login")
            
            # 填写邮箱
            email_field = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "email"))
            )
            email_field.send_keys(self.email)
            
            # 填写密码
            password_field = self.driver.find_element(By.ID, "password")
            password_field.send_keys(self.password)
            
            # 提交登录
            self.driver.find_element(By.ID, "login_submit").click()
            print("登录凭证已提交")
            time.sleep(3)  # 等待登录完成
        except Exception as e:
            print(f"Selenium登录失败: {str(e)}")
            raise

    def selenium_checkin(self):
        """Selenium签到流程"""
        try:
            self.driver.get(f"{self.base_url}/user")
            try:
                # 检查是否已签到
                checkin_status = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, '//*[@id="kt_subheader"]/div/div[2]/a'))
                ).text
                if "已签到" in checkin_status:
                    print("今日已签到，无需重复操作")
                    self.chechll()
                    return "今日已签到"
            except:
                pass
            
            # 执行签到
            print("111")
            checkin_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "checkin"))
            )
            checkin_btn.click()
            print("签到请求已发送")
            time.sleep(3)  # 等待结果加载
            
            self.chechll()
            return "签到成功"
        except Exception as e:
            print(f"Selenium签到失败: {str(e)}")
            return "签到失败"

    def requests_checkin(self):
        """标准requests签到流程"""
        session = requests.Session()
        login_url = f"{self.base_url}/auth/login"
        
        # 构造登录数据
        email_encoded = self.email.replace('@', '%40')
        payload = f'email={email_encoded}&passwd={self.password}&code='
        
        try:
            # 执行登录
            response = session.post(
                login_url,
                data=payload,
                headers={'User-Agent': 'Mozilla/5.0'},
                verify=False
            )
            response.raise_for_status()
            
            # 执行签到
            checkin_response = session.post(
                f"{self.base_url}/user/checkin",
                headers={'Referer': f"{self.base_url}/user"},
                verify=False
            )
            result = json.loads(checkin_response.text)
            return result.get('msg', '未知响应')
        except Exception as e:
            return f"Requests签到失败: {str(e)}"

    def send_notification(self, message):
        """发送结果通知"""
        # Server酱通知
        if self.sckey:
            try:
                requests.get(f'https://sctapi.ftqq.com/{self.sckey}.send?title=机场签到&desp={message}', timeout=10)
            except Exception as e:
                print(f"Server酱通知发送失败: {str(e)}")
        
        # Telegram通知
        if self.tg_bot_token and self.tg_user_id:
            try:
                requests.get(
                    f'https://api.telegram.org/bot{self.tg_bot_token}/sendMessage',
                    params={
                        'chat_id': self.tg_user_id,
                        'text': message,
                        'disable_web_page_preview': 'True'
                    },
                    timeout=10
                )
            except Exception as e:
                print(f"Telegram通知发送失败: {str(e)}")

    def run(self):
        """主执行流程"""
        result = ""
        try:
            # 判断是否需要Selenium流程
            if "fawncloud" in self.base_url:
                self.handle_slider()
                self.selenium_login()
                result = self.selenium_checkin()
            else:
                result = self.requests_checkin()
                
            print(f"最终结果：{result}")
        finally:
            # 确保关闭浏览器实例
            if hasattr(self, 'driver'):
                self.driver.quit()
        
        # 发送通知（如果配置了相关参数）
        if any([self.sckey, self.tg_bot_token]):
            self.send_notification(result)

    def chechll(self):
        # 获取流量信息
        print("获取流量信息")
        remaining = self.driver.find_element(By.XPATH, '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[1]/div/div/div/strong').text
        print(f"剩余流量：{remaining}")
        return

if __name__ == "__main__":
    try:
        
        manager = CheckinManager()
        manager.run()
    except Exception as e:
        print(f"程序执行出错: {str(e)}")
        exit(1)