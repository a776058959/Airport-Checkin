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
        
        # 确保 base_url 格式正确
        if not self.base_url.startswith(('http://', 'https://')):
            self.base_url = 'https://' + self.base_url
            print(f"自动添加协议前缀: {self.base_url}")
        
        # 从环境变量获取通知配置（可选）
        self.sckey = os.environ.get('SCKEY', '')
        self.tg_bot_token = os.environ.get('TGBOT', '')
        self.tg_user_id = os.environ.get('TGUSERID', '')
        
        # 初始化Selenium配置
        self.driver = self._init_selenium()
        
    def _init_selenium(self):
        """初始化Selenium WebDriver（无头模式）"""
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless")          # 无头模式
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-ssl-errors')
        
        return webdriver.Chrome(options=chrome_options)

    # ==================== 滑块验证（已注释，保留备用） ====================
    # def handle_slider(self):
    #     try:
    #         print("正在检测滑块验证...")
    #         self.driver.get(self.base_url)
    #         slider = WebDriverWait(self.driver, 5).until(
    #             EC.presence_of_element_located((By.ID, "handler"))
    #         )
    #         print("检测到滑块，开始模拟滑动...")
    #         actions = ActionChains(self.driver)
    #         actions.click_and_hold(slider)
    #         actions.move_by_offset(322, 0)
    #         actions.release().perform()
    #         print("滑块验证完成")
    #         time.sleep(2)
    #     except Exception as e:
    #         print(f"滑块处理异常（可能无需验证）: {str(e)}")

    # ==================== Cloudflare 验证检测与处理 ====================
    def is_cloudflare_challenge(self):
        """通过URL判断是否在Cloudflare验证页面"""
        current_url = self.driver.current_url
        indicators = ["challenge", "cdn-cgi", "turnstile", "cloudflare", "waiting-room", "verification", "protected"]
        for ind in indicators:
            if ind in current_url.lower():
                print(f"检测到Cloudflare验证URL特征: {ind}")
                return True
        return False

    def check_page_title_for_challenge(self):
        """通过页面标题判断是否在验证页面"""
        title = self.driver.title.lower()
        challenge_titles = ["just a moment", "checking your browser", "verifying", "please wait", "redirecting", "security check", "ddos protection"]
        for ct in challenge_titles:
            if ct in title:
                print(f"检测到验证页面标题: {title}")
                return True
        return False

    def check_page_content_for_challenge(self):
        """通过页面内容判断是否在验证页面"""
        try:
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            phrases = ["checking your browser", "just a moment", "verifying you are human", "please wait", "ddos protection", "cloudflare", "turnstile"]
            for phrase in phrases:
                if phrase in page_text:
                    print(f"检测到验证页面内容: {phrase}")
                    return True
            # 检查特定元素
            selectors = [
                '//*[contains(@class, "cf-browser-verification")]',
                '//*[contains(@id, "challenge-form")]',
                '//*[contains(text(), "Verifying")]',
                '//iframe[contains(@src, "cloudflare")]'
            ]
            for sel in selectors:
                try:
                    el = self.driver.find_element(By.XPATH, sel)
                    if el.is_displayed():
                        print(f"检测到验证页面元素: {sel}")
                        return True
                except:
                    continue
            return False
        except Exception as e:
            print(f"检查页面内容时出错: {e}")
            return False

    def is_in_cloudflare_challenge(self):
        """综合判断是否在Cloudflare验证阶段"""
        if self.is_cloudflare_challenge():
            return True
        if self.check_page_title_for_challenge():
            return True
        if self.check_page_content_for_challenge():
            return True
        try:
            # 检查页面加载状态
            if self.driver.execute_script("return document.readyState") != "complete":
                print("页面仍在加载，可能是验证页面")
                return True
        except:
            pass
        return False

    def handle_cloudflare_simple_wait(self):
        """简单等待验证通过（最多25秒）"""
        try:
            print("等待Cloudflare验证自动通过...")
            for i in range(8):  # 25秒 / 3秒 ≈ 8次
                if not self.is_in_cloudflare_challenge():
                    print("Cloudflare验证已自动通过！")
                    return True
                print(f"等待中... ({i*3+3}/25秒)")
                time.sleep(3)
            print("简单等待超时")
            return False
        except Exception as e:
            print(f"简单等待失败: {e}")
            return False

    def handle_cloudflare_navigation(self):
        """通过导航离开再返回来触发自动验证"""
        try:
            print("通过导航策略处理Cloudflare验证...")
            challenge_url = self.driver.current_url
            self.driver.get("about:blank")
            time.sleep(3)
            print("等待验证在后台处理...")
            time.sleep(12)
            self.driver.get(challenge_url)
            time.sleep(5)
            if not self.is_in_cloudflare_challenge():
                print("Cloudflare验证已通过！")
                return True
            else:
                print("验证可能仍在进行，尝试刷新...")
                self.driver.refresh()
                time.sleep(5)
                return not self.is_in_cloudflare_challenge()
        except Exception as e:
            print(f"导航策略失败: {e}")
            return False

    def handle_cloudflare_comprehensive(self):
        """综合处理Cloudflare验证（最多尝试3次）"""
        for attempt in range(3):
            print(f"第 {attempt+1} 次尝试处理Cloudflare验证...")
            if self.handle_cloudflare_simple_wait():
                return True
            if self.handle_cloudflare_navigation():
                return True
            print("刷新页面重试...")
            self.driver.refresh()
            time.sleep(8)
            if not self.is_in_cloudflare_challenge():
                return True
        print("经过3次尝试仍无法通过验证")
        return False

    def handle_cloudflare_if_needed(self):
        """如果需要，处理Cloudflare验证"""
        if self.is_in_cloudflare_challenge():
            print("检测到Cloudflare验证，开始处理...")
            return self.handle_cloudflare_comprehensive()
        else:
            print("无需处理Cloudflare验证")
            return True

    # ==================== 登录和签到 ====================
    def selenium_login(self):
        """Selenium登录流程"""
        try:
            login_url = f"{self.base_url}/auth/login"
            print(f"正在访问登录页面: {login_url}")
            self.driver.get(login_url)
            
            # 填写邮箱
            email_field = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "email"))
            )
            print("填写邮箱...")
            email_field.send_keys(self.email)
            
            # 填写密码
            password_field = self.driver.find_element(By.ID, "password")
            password_field.send_keys(self.password)
            print("填写密码...")
            
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
            # 先尝试获取流量信息
            self.check_traffic()
            
            # 访问用户页面
            user_url = f"{self.base_url}/user"
            print(f"访问用户页面: {user_url}")
            self.driver.get(user_url)
            
            try:
                # 检查是否已签到
                print("检查是否已签到...")
                checkin_status = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, '//*[@id="kt_subheader"]/div/div[2]/a'))
                ).text
                if "已签到" in checkin_status:
                    print("今日已签到，无需重复操作")
                    self.check_traffic()
                    return "今日已签到"
            except:
                print("今日未签到...")
                pass
            
            # 执行签到
            print("执行签到")
            checkin_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//*[@id="checkin"]'))
            )
            checkin_btn.click()
            print("签到请求已发送")
            time.sleep(5)
            
            self.driver.refresh()
            time.sleep(3)
            self.check_traffic()
            return "签到成功"
        except Exception as e:
            print(f"Selenium签到失败: {str(e)}")
            return f"签到失败: {str(e)}"

    def requests_checkin(self):
        """标准requests签到流程（备用）"""
        session = requests.Session()
        login_url = f"{self.base_url}/auth/login"
        email_encoded = self.email.replace('@', '%40')
        payload = f'email={email_encoded}&passwd={self.password}&code='
        try:
            response = session.post(login_url, data=payload,
                                    headers={'User-Agent': 'Mozilla/5.0'},
                                    verify=False, timeout=30)
            response.raise_for_status()
            checkin_response = session.post(f"{self.base_url}/user/checkin",
                                            headers={'Referer': f"{self.base_url}/user"},
                                            verify=False, timeout=30)
            result = json.loads(checkin_response.text)
            return result.get('msg', '未知响应')
        except Exception as e:
            return f"Requests签到失败: {str(e)}"

    def send_notification(self, message):
        """发送结果通知"""
        if self.sckey:
            try:
                requests.get(f'https://sctapi.ftqq.com/{self.sckey}.send?title=机场签到&desp={message}', timeout=10)
            except Exception as e:
                print(f"Server酱通知发送失败: {str(e)}")
        if self.tg_bot_token and self.tg_user_id:
            try:
                requests.get(f'https://api.telegram.org/bot{self.tg_bot_token}/sendMessage',
                             params={'chat_id': self.tg_user_id, 'text': message},
                             timeout=10)
            except Exception as e:
                print(f"Telegram通知发送失败: {str(e)}")

    def run(self):
        """主执行流程"""
        result = ""
        try:
            # 判断站点类型
            if "skyvpn" in self.base_url:
                # 注释掉滑块验证
                # self.handle_slider()
                self.selenium_login()
                result = self.selenium_checkin()
            elif "fawncloud" in self.base_url:
                print(f"访问网站: {self.base_url}")
                self.driver.get(self.base_url)
                cloudflare_ok = self.handle_cloudflare_if_needed()
                if not cloudflare_ok:
                    print("Cloudflare验证处理失败，但仍尝试继续...")
                self.selenium_login()
                result = self.selenium_checkin()
            else:
                result = self.requests_checkin()
                
            print(f"最终结果：{result}")
        except Exception as e:
            print(f"执行过程中发生错误: {str(e)}")
            result = f"执行错误: {str(e)}"
        finally:
            if hasattr(self, 'driver'):
                self.driver.quit()
        
        if any([self.sckey, self.tg_bot_token]):
            self.send_notification(result)
        return result

    def check_traffic(self):
        """获取流量信息（容错）"""
        try:
            print("获取流量信息")
            remaining = self.driver.find_element(By.XPATH,
                '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[1]/div/div/div/strong').text
            print(f"剩余流量：{remaining}")
            return remaining
        except Exception as e:
            print(f"获取流量信息失败: {str(e)}")
            return "获取失败"

if __name__ == "__main__":
    try:
        manager = CheckinManager()
        manager.run()
    except Exception as e:
        print(f"程序执行出错: {str(e)}")
        exit(1)