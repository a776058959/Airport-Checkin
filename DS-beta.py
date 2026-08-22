import os
import time
import json
import random
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

requests.packages.urllib3.disable_warnings()

class CheckinManager:
    def __init__(self):
        # 必须从环境变量读取敏感信息
        self.email = os.environ.get('EMAIL')
        self.password = os.environ.get('PASSWORD')
        self.base_url = os.environ.get('BASE_URL')

        if not all([self.email, self.password, self.base_url]):
            raise ValueError("必须设置环境变量 EMAIL, PASSWORD, BASE_URL")

        # 确保 base_url 格式正确
        if not self.base_url.startswith(('http://', 'https://')):
            self.base_url = 'https://' + self.base_url
            print(f"自动添加协议前缀: {self.base_url}")

        # 自动检测无头模式：优先环境变量，否则根据 GITHUB_ACTIONS 判断
        headless_env = os.environ.get('HEADLESS')
        if headless_env is not None:
            self.headless = headless_env.lower() == 'true'
        else:
            self.headless = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
        print(f"无头模式: {'开启' if self.headless else '关闭'}")

        # Cloudflare 点击坐标（支持环境变量覆盖）
        coord_str = os.environ.get('CLICK_COORD', '')
        if coord_str:
            try:
                x, y = map(int, coord_str.split(','))
                self.click_coord = (x, y)
                print(f"使用环境变量指定点击坐标: {self.click_coord}")
            except:
                coord_str = ''
                print("环境变量 CLICK_COORD 格式错误，回退到内置默认坐标")

        if not coord_str:
            if "skyvpn" in self.base_url:
                self.click_coord = (430, 310)   # skyvpnx.com 默认坐标
            else:
                self.click_coord = (800, 550)   # 通用默认坐标
            print(f"使用默认 Cloudflare 点击坐标: {self.click_coord}")

        # 通知配置
        self.sckey = os.environ.get('SCKEY', '')
        self.tg_bot_token = os.environ.get('TGBOT', '')
        self.tg_user_id = os.environ.get('TGUSERID', '')

        # Playwright 相关
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def _init_browser(self):
        print("初始化 Playwright 浏览器...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1600,900",
            ],
        )
        self.context = self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            permissions=["geolocation"],
        )

        # 注入反检测脚本
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        if HAS_STEALTH:
            stealth_sync(self.context)
            print("已应用 playwright-stealth")

        self.page = self.context.new_page()
        print("浏览器已启动，视口为 1600x900")

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    # ==================== Cloudflare 检测 ====================
    def is_cloudflare_challenge(self):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except:
            pass
        try:
            title = self.page.title().lower()
            url = self.page.url.lower()
            try:
                content = self.page.content().lower()
            except:
                content = ""
        except:
            return False

        if "just a moment" in title:
            return True
        if "cloudflare" in url or "challenge" in url or "cdn-cgi" in url:
            return True
        if content:
            if "window._cf_chl_opt" in content or "challenge-platform" in content or "turnstile" in content:
                return True
        try:
            body_text = self.page.inner_text("body").lower()
            if "请验证您是真人" in body_text or "verify you are human" in body_text:
                return True
        except:
            pass
        return False

    # ==================== 人类行为模拟 ====================
    def _simulate_human_mouse(self):
        try:
            x, y = self.click_coord
            offset_x = random.randint(-50, 50)
            offset_y = random.randint(-50, 50)
            self.page.mouse.move(x + offset_x, y + offset_y)
            time.sleep(random.uniform(0.2, 0.5))
            self.page.mouse.move(x, y)
            time.sleep(random.uniform(0.1, 0.3))
        except:
            pass

    def _simulate_human_scroll(self):
        try:
            scroll_y = random.randint(0, 100)
            self.page.mouse.wheel(0, scroll_y)
            time.sleep(0.3)
            self.page.mouse.wheel(0, -scroll_y)
        except:
            pass

    # ==================== 点击策略 ====================
    def _click_by_coordinates(self):
        x, y = self.click_coord
        print(f"尝试坐标点击: ({x}, {y})")
        self._simulate_human_mouse()
        time.sleep(random.uniform(0.5, 1.5))
        try:
            self.page.mouse.click(x, y)
            return True
        except Exception as e:
            print(f"坐标点击失败: {e}")
            return False

    def _click_via_js_recursive(self):
        js_code = """
        () => {
            function findAndClick(root) {
                let exact = root.querySelectorAll('input[type="checkbox"][aria-label="请验证您是真人"]');
                if (exact.length > 0) {
                    let el = exact[0];
                    try { el.click(); return true; } catch(e) {
                        let rect = el.getBoundingClientRect();
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX: rect.x+rect.width/2, clientY: rect.y+rect.height/2}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX: rect.x+rect.width/2, clientY: rect.y+rect.height/2}));
                        el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX: rect.x+rect.width/2, clientY: rect.y+rect.height/2}));
                        return true;
                    }
                }
                let checkboxes = root.querySelectorAll('input[type="checkbox"]');
                for (let cb of checkboxes) {
                    if (cb.offsetWidth > 0 || cb.offsetHeight > 0) {
                        cb.click();
                        return true;
                    }
                }
                let allElements = root.querySelectorAll('*');
                for (let el of allElements) {
                    if (el.innerText && el.innerText.includes('请验证您是真人')) {
                        let target = el.closest('label') || el.parentElement;
                        if (target) {
                            target.click();
                            return true;
                        }
                    }
                }
                for (let el of allElements) {
                    if (el.shadowRoot) {
                        if (findAndClick(el.shadowRoot)) return true;
                    }
                }
                return false;
            }
            return findAndClick(document);
        }
        """
        try:
            result = self.page.evaluate(js_code)
            if result:
                print("✅ JS 递归点击成功（含 Shadow DOM）")
                return True
            else:
                print("JS 递归未找到可点击元素")
                return False
        except Exception as e:
            print(f"JS 递归点击异常: {e}")
            return False

    def _click_via_standard_selectors(self):
        selectors = [
            'input[type="checkbox"][aria-label="请验证您是真人"]',
            'input[type="checkbox"]',
            'div[role="checkbox"]',
            '.cf-turnstile',
            '.turnstile',
            '[data-sitekey]',
        ]
        # 主文档
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=1000):
                    locator.click(timeout=3000)
                    print(f"✅ 主文档 CSS 选择器点击成功: {selector}")
                    return True
            except Exception:
                continue
        # iframe
        try:
            for frame in self.page.frames:
                for selector in selectors:
                    try:
                        locator = frame.locator(selector).first
                        if locator.is_visible(timeout=1000):
                            locator.click(timeout=3000)
                            print(f"✅ iframe CSS 选择器点击成功: {selector} (frame: {frame.url})")
                            return True
                    except Exception:
                        continue
        except Exception as e:
            print(f"iframe 遍历异常: {e}")
        return False

    def _click_via_xpath(self):
        exact_xpath = "/html/body//div/div/div[1]/div/label/input"
        # 主文档
        try:
            locator = self.page.locator(f"xpath={exact_xpath}")
            if locator.count() > 0:
                locator.first.click(timeout=3000)
                print("✅ 精确 XPath 点击成功（主文档）")
                return True
        except Exception:
            pass
        # iframe
        try:
            for frame in self.page.frames:
                locator = frame.locator(f"xpath={exact_xpath}")
                if locator.count() > 0:
                    locator.first.click(timeout=3000)
                    print(f"✅ 精确 XPath 点击成功（iframe: {frame.url}）")
                    return True
        except Exception:
            pass
        return False

    def _click_by_text_coordinates(self):
        try:
            text_locator = self.page.locator('text="请验证您是真人"').first
            if text_locator.is_visible(timeout=1000):
                box = text_locator.bounding_box()
                if box:
                    self.page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                    print("✅ 文本坐标点击成功")
                    return True
        except Exception:
            pass
        return False

    def try_click_turnstile(self):
        print("[策略0] 尝试坐标点击...")
        if self._click_by_coordinates():
            return True

        print("[策略1] JS 递归穿透 Shadow DOM...")
        if self._click_via_js_recursive():
            return True

        print("[策略2] 尝试用户提供的精确 XPath...")
        if self._click_via_xpath():
            return True

        print("[策略3] 尝试标准 CSS 选择器...")
        if self._click_via_standard_selectors():
            return True

        print("[策略4] 尝试文本坐标点击...")
        if self._click_by_text_coordinates():
            return True

        print("所有自动点击策略均失败")
        return False

    def handle_cloudflare(self):
        print("检测到 Cloudflare 验证，开始处理...")
        self.page.wait_for_timeout(5000)

        for attempt in range(5):
            if not self.is_cloudflare_challenge():
                print("✅ Cloudflare 验证已通过！")
                return True

            print(f"\n第 {attempt+1} 次尝试点击...")
            self._simulate_human_scroll()
            if self.try_click_turnstile():
                print("点击成功，等待验证结果...")
                wait_time = random.uniform(5, 8)
                self.page.wait_for_timeout(int(wait_time * 1000))
                if not self.is_cloudflare_challenge():
                    print("✅ Cloudflare 验证已通过！")
                    return True
            else:
                print("点击失败，等待后重试...")
                self.page.wait_for_timeout(3000)

        if not self.headless:
            print("自动处理未通过，请在浏览器中手动完成验证，然后按 Enter 继续...")
            input(">>> ")
            for _ in range(30):
                if not self.is_cloudflare_challenge():
                    print("✅ 手动验证完成！")
                    return True
                self.page.wait_for_timeout(2000)

        print("❌ Cloudflare 验证未通过")
        return False

    # ==================== 登录和签到 ====================
    def login(self, base_url=None):
        if base_url is None:
            base_url = self.base_url
        login_url = f"{base_url}/auth/login"
        print(f"正在访问登录页面: {login_url}")
        self.page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

        if self.is_cloudflare_challenge():
            if not self.handle_cloudflare():
                raise Exception("Cloudflare 验证未通过")
            self.page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

        try:
            self.page.fill("#email", self.email)
            self.page.fill("#password", self.password)
            self.page.click("#login_submit")
            print("登录凭证已提交")
            self.page.wait_for_timeout(3000)
            return True
        except Exception as e:
            print(f"登录表单操作失败: {e}")
            self.page.screenshot(path="login_fail.png")
            raise

    def checkin(self, base_url=None):
        if base_url is None:
            base_url = self.base_url
        user_url = f"{base_url}/user"
        print(f"访问用户页面: {user_url}")
        self.page.goto(user_url, wait_until="domcontentloaded", timeout=60000)

        try:
            status_element = self.page.locator('//*[@id="kt_subheader"]/div/div[2]/a')
            if status_element.is_visible(timeout=5000):
                status_text = status_element.inner_text()
                if "已签到" in status_text:
                    print("今日已签到，无需重复操作")
                    self.check_traffic()
                    return "今日已签到"
        except:
            print("今日未签到...")

        try:
            self.page.click('//*[@id="checkin"]', timeout=10000)
            print("签到请求已发送")
            self.page.wait_for_timeout(5000)
            self.page.reload()
            self.page.wait_for_timeout(3000)
            self.check_traffic()
            return "签到成功"
        except Exception as e:
            print(f"签到操作失败: {e}")
            return f"签到失败: {e}"

    def check_traffic(self):
        try:
            traffic_selector = '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[1]/div/div/div/strong'
            element = self.page.locator(traffic_selector)
            if element.is_visible(timeout=5000):
                remaining = element.inner_text()
                print(f"剩余流量：{remaining}")
                return remaining
            else:
                print("未找到流量信息")
                return "获取失败"
        except Exception as e:
            print(f"获取流量信息失败: {e}")
            return "获取失败"

    def send_notification(self, message):
        if self.sckey:
            try:
                requests.get(f'https://sctapi.ftqq.com/{self.sckey}.send?title=机场签到&desp={message}', timeout=10)
            except:
                pass
        if self.tg_bot_token and self.tg_user_id:
            try:
                requests.get(f'https://api.telegram.org/bot{self.tg_bot_token}/sendMessage',
                             params={'chat_id': self.tg_user_id, 'text': message},
                             timeout=10)
            except:
                pass

    # ==================== 主流程 ====================
    def run(self):
        result = ""
        try:
            self._init_browser()

            if "skyvpn" in self.base_url:
                self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
                if self.is_cloudflare_challenge():
                    if not self.handle_cloudflare():
                        print("Cloudflare 验证失败，但尝试继续...")
                self.login()
                result = self.checkin()

            elif "skyxcloud" in self.base_url:
                print("打开分流网址:", self.base_url)
                self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
                self.page.wait_for_timeout(random.randint(3000, 5000))

                print("点击防墙分发按钮 (800, 560)")
                self.page.mouse.click(800, 560)
                self.page.wait_for_timeout(random.randint(3000, 5000))

                try:
                    self.page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass

                actual_url = self.page.url
                print("分发后的实际网址:", actual_url)
                actual_base = actual_url.rstrip('/')

                if actual_base == "https://www.skyxcloud.cc":
                    print("URL 未改变，可能点击未生效，请检查坐标或手动处理。")
                else:
                    print("点击登录按钮 (310, 518)")
                    self.page.mouse.click(310, 518)
                    self.page.wait_for_timeout(random.randint(3000, 5000))
                    try:
                        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except:
                        pass

                    if self.is_cloudflare_challenge():
                        if not self.handle_cloudflare():
                            print("Cloudflare 验证失败，但尝试继续...")
                            self.page.goto(f"{actual_base}/auth/login", wait_until="domcontentloaded", timeout=60000)
                            if self.is_cloudflare_challenge():
                                self.handle_cloudflare()

                    self.login(base_url=actual_base)
                    result = self.checkin(base_url=actual_base)

            else:
                result = "暂不支持该站点"

            print(f"最终结果：{result}")
        except Exception as e:
            print(f"执行过程中发生错误: {str(e)}")
            result = f"执行错误: {str(e)}"
        finally:
            self.close()

        if any([self.sckey, self.tg_bot_token]):
            self.send_notification(result)
        return result

if __name__ == "__main__":
    try:
        manager = CheckinManager()
        manager.run()
    except Exception as e:
        print(f"程序执行出错: {str(e)}")
        import sys
        sys.exit(1)