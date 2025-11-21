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
        #chrome_options.add_argument("--headless")  # 无头模式

        # 基础选项
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        # 反检测选项
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        # 用户代理
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0")

        return webdriver.Chrome(options=chrome_options)

    def handle_slider(self):
        #处理滑块验证（专用于skyvpn）
        time.sleep(2)
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
            time.sleep(4)  # 等待页面跳转
        except Exception as e:
            print(f"滑块处理异常（可能无需验证）: {str(e)}")

    def is_cloudflare_challenge(self):
        """通过URL判断是否在Cloudflare验证页面"""
        current_url = self.driver.current_url
        cloudflare_indicators = [
            "challenge",           # 最常见的标识
            "cdn-cgi",             # Cloudflare CDN
            "turnstile",           # Cloudflare Turnstile
            "cloudflare",          # 直接包含cloudflare
            "waiting-room",        # 等待室
            "verification",        # 验证
            "protected",           # 受保护
        ]
        
        for indicator in cloudflare_indicators:
            if indicator in current_url.lower():
                print(f"检测到Cloudflare验证URL特征: {indicator}")
                return True
        return False

    def check_page_title_for_challenge(self):
        """通过页面标题判断是否在验证页面"""
        title = self.driver.title.lower()
        challenge_titles = [
            "just a moment",
            "checking your browser",
            "verifying",
            "please wait",
            "redirecting",
            "security check",
            "ddos protection",
        ]
        
        for challenge_title in challenge_titles:
            if challenge_title in title:
                print(f"检测到验证页面标题: {title}")
                return True
        return False

    def check_page_content_for_challenge(self):
        """通过页面内容判断是否在验证页面"""
        try:
            # 获取页面文本内容
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            
            challenge_phrases = [
                "checking your browser",
                "just a moment",
                "verifying you are human",
                "please wait",
                "ddos protection",
                "cloudflare",
                "turnstile",
            ]
            
            for phrase in challenge_phrases:
                if phrase in page_text:
                    print(f"检测到验证页面内容: {phrase}")
                    return True
                    
            # 检查特定的元素
            challenge_selectors = [
                '//*[contains(@class, "cf-browser-verification")]',
                '//*[contains(@id, "challenge-form")]',
                '//*[contains(text(), "Verifying")]',
                '//iframe[contains(@src, "cloudflare")]',
            ]
            
            for selector in challenge_selectors:
                try:
                    element = self.driver.find_element(By.XPATH, selector)
                    if element.is_displayed():
                        print(f"检测到验证页面元素: {selector}")
                        return True
                except:
                    continue
                    
            return False
            
        except Exception as e:
            print(f"检查页面内容时出错: {e}")
            return False

    def is_in_cloudflare_challenge(self):
        """综合判断是否在Cloudflare验证阶段"""
        methods = [
            self.is_cloudflare_challenge,           # URL检测
            self.check_page_title_for_challenge,    # 标题检测
            self.check_page_content_for_challenge,  # 内容检测
        ]
        
        for method in methods:
            if method():
                return True
        
        # 额外检查：页面是否有长时间加载或重定向
        try:
            # 检查页面状态
            page_state = self.driver.execute_script("return document.readyState")
            if page_state != "complete":
                print("页面仍在加载，可能是验证页面")
                return True
        except:
            pass
            
        return False

    def handle_cloudflare_simple_wait(self):
        """简单等待验证通过"""
        try:
            print("等待Cloudflare验证自动通过...")
            
            wait_time = 25  # 等待25秒
            check_interval = 3  # 每3秒检查一次
            
            for i in range(wait_time // check_interval):
                if not self.is_in_cloudflare_challenge():
                    print("Cloudflare验证已自动通过！")
                    return True
                    
                print(f"等待中... ({(i + 1) * check_interval}/{wait_time}秒)")
                time.sleep(check_interval)
            
            print("简单等待超时")
            return False
            
        except Exception as e:
            print(f"简单等待失败: {e}")
            return False

    def handle_cloudflare_navigation(self):
        """通过导航离开再返回来触发自动验证"""
        try:
            print("通过导航策略处理Cloudflare验证...")
            
            # 记录当前URL（验证页面）
            challenge_url = self.driver.current_url
            
            # 导航到其他页面
            print("导航到空白页...")
            self.driver.get("about:blank")
            time.sleep(3)
            
            # 等待一段时间
            print("等待验证在后台处理...")
            time.sleep(12)
            
            # 返回原页面
            print("返回原页面...")
            self.driver.get(challenge_url)
            time.sleep(5)
            
            # 检查验证是否通过
            if not self.is_in_cloudflare_challenge():
                print("Cloudflare验证已通过！")
                return True
            else:
                print("验证可能仍在进行，尝试刷新...")
                self.driver.refresh()
                time.sleep(5)
                
                # 再次检查
                return not self.is_in_cloudflare_challenge()
                
        except Exception as e:
            print(f"导航策略失败: {e}")
            return False

    def handle_cloudflare_comprehensive(self):
        """综合处理Cloudflare验证"""
        try:
            max_attempts = 5
            for attempt in range(max_attempts):
                print(f"第 {attempt + 1} 次尝试处理Cloudflare验证...")
                
                # 策略1: 简单等待
                if self.handle_cloudflare_simple_wait():
                    return True
                    
                # 策略2: 导航离开再返回
                if self.handle_cloudflare_navigation():
                    return True
                    
                # 策略3: 刷新页面
                print("刷新页面重试...")
                self.driver.refresh()
                time.sleep(8)
                
                # 检查是否还在验证页面
                if not self.is_in_cloudflare_challenge():
                    return True
            
            print(f"经过 {max_attempts} 次尝试仍无法通过验证")
            return False
            
        except Exception as e:
            print(f"处理Cloudflare验证时出错: {e}")
            return False

    def handle_cloudflare_if_needed(self):
        """如果需要，处理Cloudflare验证"""
        if self.is_in_cloudflare_challenge():
            print("检测到Cloudflare验证，开始处理...")
            return self.handle_cloudflare_comprehensive()
        else:
            print("无需处理Cloudflare验证")
            return True

    def selenium_login(self):
        """Selenium登录流程"""
        try:
            print("正在执行Selenium登录...")
            self.driver.get(f"{self.base_url}/auth/login")
            
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
        time.sleep(3)
        self.chechll()
        try:
            self.driver.get(f"{self.base_url}/user")
            try:
                # 检查是否已签到
                print("检查是否已签到...")
                checkin_status = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, '//*[@id="kt_subheader"]/div/div[2]/a'))
                ).text
                if "已签到" in checkin_status:
                    print("今日已签到，无需重复操作")
                    self.chechll()
                    return "今日已签到"
                else:
                    print("今日未签到...")
            except:
                print("报错，今日未签到...")
                pass
            
            # 执行签到
            print("执行签到")
            checkin_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//*[@id="checkin"]'))
            )
            checkin_btn.click()
            print("签到请求已发送")
            time.sleep(5)  # 等待结果加载
            print("已经等待结果加载5秒")
            print("刷新页面")
            self.driver.refresh()
            time.sleep(3)
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
            if "skyvpn" in self.base_url:
                # 滑块验证
                self.handle_slider()
                self.selenium_login()
                result = self.selenium_checkin()
            elif "fawncloud" in self.base_url:
                # 对于fawncloud，先访问主页并处理可能的Cloudflare验证
                print(f"访问网站: {self.base_url}")
                self.driver.get(self.base_url)
                
                # 处理可能的Cloudflare验证
                cloudflare_result = self.handle_cloudflare_if_needed()
                if not cloudflare_result:
                    print("Cloudflare验证处理失败，但仍尝试继续...")
                
                # 等待验证通过后，进行登录和签到
                self.selenium_login()
                result = self.selenium_checkin()
            else:
                # 其他网站使用Requests方式
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