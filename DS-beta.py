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
        self.email = os.environ.get('EMAIL')
        self.password = os.environ.get('PASSWORD')
        self.base_url = os.environ.get('BASE_URL')

        if not all([self.email, self.password, self.base_url]):
            raise ValueError("必须设置环境变量 EMAIL, PASSWORD, BASE_URL")

        if not self.base_url.startswith(('http://', 'https://')):
            self.base_url = 'https://' + self.base_url
            print(f"自动添加协议前缀: {self.base_url}")

        # 无头模式自动检测
        headless_env = os.environ.get('HEADLESS')
        if headless_env is not None:
            self.headless = headless_env.lower() == 'true'
        else:
            self.headless = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
        print(f"无头模式: {'开启' if self.headless else '关闭'}")

        # Cloudflare 点击坐标
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
                self.click_coord = (430, 310)
            else:
                self.click_coord = (800, 550)
            print(f"使用默认 Cloudflare 点击坐标: {self.click_coord}")

        # 通知配置
        self.sckey = os.environ.get('SCKEY', '')
        self.tg_bot_token = os.environ.get('TGBOT', '')
        self.tg_user_id = os.environ.get('TGUSERID', '')

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
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=1000):
                    locator.click(timeout=3000)
                    print(f"✅ 主文档 CSS 选择器点击成功: {selector}")
                    return True
            except Exception:
                continue
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
        try:
            locator = self.page.locator(f"xpath={exact_xpath}")
            if locator.count() > 0:
                locator.first.click(timeout=3000)
                print("✅ 精确 XPath 点击成功（主文档）")
                return True
        except Exception:
            pass
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

    # ==================== 登录（拟人化输入） ====================
    def login(self, base_url=None):
        if base_url is None:
            base_url = self.base_url
        login_url = f"{base_url}/auth/login"
        print(f"正在访问登录页面: {login_url}")
        self.page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

        # 处理可能的 Cloudflare 验证
        if self.is_cloudflare_challenge():
            if not self.handle_cloudflare():
                raise Exception("Cloudflare 验证未通过")
            self.page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

        # 访问登录页后等待 3~5 秒
        wait_after_load = random.uniform(3, 5)
        print(f"等待 {wait_after_load:.2f} 秒后开始填写表单...")
        self.page.wait_for_timeout(int(wait_after_load * 1000))

        try:
            # 定位邮箱输入框，滚动到可见并悬停
            email_locator = self.page.locator("#email")
            email_locator.scroll_into_view_if_needed()
            email_locator.hover()
            self.page.wait_for_timeout(random.randint(300, 600))

            # 点击输入框，然后逐字符输入
            email_locator.click()
            self.page.type("#email", self.email, delay=random.randint(50, 150))
            print("邮箱输入完成")
            self.page.wait_for_timeout(random.randint(500, 1000))

            # 定位密码输入框
            password_locator = self.page.locator("#password")
            password_locator.scroll_into_view_if_needed()
            password_locator.hover()
            self.page.wait_for_timeout(random.randint(300, 600))
            password_locator.click()
            self.page.type("#password", self.password, delay=random.randint(50, 150))
            print("密码输入完成")
            self.page.wait_for_timeout(random.randint(500, 1000))

            # 登录按钮：悬停后点击
            login_btn = self.page.locator("#login_submit")
            login_btn.hover()
            self.page.wait_for_timeout(random.randint(500, 1200))
            login_btn.click()
            print("登录凭证已提交")

            # 提交后等待响应
            self.page.wait_for_timeout(random.randint(3000, 5000))
            return True

        except Exception as e:
            print(f"登录表单操作失败: {e}")
            self.page.screenshot(path="login_fail.png")
            raise

    # ==================== 签到 ====================
    def checkin(self, base_url=None):
        if base_url is None:
            base_url = self.base_url
        user_url = f"{base_url}/user"
        print(f"访问用户页面: {user_url}")
        self.page.goto(user_url, wait_until="domcontentloaded", timeout=60000)
        # 等待页面动态内容加载
        self.page.wait_for_timeout(3000)

        # 检查整个页面是否包含“已签到”
        try:
            body_text = self.page.inner_text("body")
            if "已签到" in body_text or "已经签到" in body_text or "今日已签到" in body_text:
                print("页面显示已签到，无需操作")
                self.check_traffic()
                return "今日已签到"
        except Exception as e:
            print(f"检查已签到文本失败: {e}")

        # 尝试多种签到按钮选择器
        selectors = [
            '#checkin',
            '//*[@id="checkin"]',
            'button:has-text("签到")',
            'a:has-text("签到")',
            '//*[contains(@class, "checkin")]',
            '//*[contains(text(), "签到")]',
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=5000):
                    locator.scroll_into_view_if_needed()
                    locator.hover()
                    self.page.wait_for_timeout(random.randint(300, 800))
                    locator.click(timeout=5000)
                    print(f"签到按钮已点击: {selector}")
                    self.page.wait_for_timeout(5000)
                    self.page.reload()
                    self.page.wait_for_timeout(3000)
                    self.check_traffic()
                    return "签到成功"
            except Exception as e:
                print(f"尝试选择器 {selector} 失败: {e}")
                continue

        # 保存截图
        try:
            self.page.screenshot(path="checkin_fail.png")
            print("已保存截图: checkin_fail.png")
        except:
            pass
        print("签到失败：未找到签到按钮")
        return "签到失败：未找到签到按钮"

    def check_traffic(self):
        try:
            # 尝试多种流量选择器
            selectors = [
                '//*[@id="kt_content"]/div[2]/div/div[2]/div[2]/div/div[1]/div/div/div/strong',
                '//strong[contains(text(), "GB")]',
                '//*[contains(text(), "剩余流量")]',
            ]
            for sel in selectors:
                try:
                    element = self.page.locator(sel).first
                    if element.is_visible(timeout=3000):
                        remaining = element.inner_text()
                        print(f"剩余流量：{remaining}")
                        return remaining
                except:
                    continue
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