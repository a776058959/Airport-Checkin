#!/usr/bin/env python3
"""机场面板自动签到（通用版）。

不同机场站的入口结构不一样，本脚本不写死任何一家，而是**先探测、再决定走哪条路**：

    A. 直连型：域名本身就是面板，过完人机验证直接就能登录签到
       例：https://www.skyvpnx.com/
    B. 跳转型：入口站只是个中转，过验证后要提交表单跳到另一个域名
       例：https://www.skyxcloud.cc/ → https://<随机域名>.skyvpn.bond/

所以脚本对每个站点都按同样的顺序尝试，区别只在于「当前域名能不能直接登录」：

    1. 显式配置的 PANEL_URL
    2. 上次缓存的面板域名
    3. 当前 BASE_URL 本身（探测它是否直接就是面板）
    4. 过人机验证 + 跟随跳转/提交表单，拿到真正的面板地址

人机验证只处理 GoEdge WAF 的滑动验证码：页面里只有 `GOEDGE_WAF_CAPTCHA_ID`
隐藏字段与写死在 JS 里的 `nonce`，服务端只比对这两个值、不做轨迹校验，
所以纯 requests 就能过，不需要浏览器自动化。

环境变量：
    EMAIL       登录邮箱（必需）
    PASSWORD    登录密码（必需）
    BASE_URL    站点地址，默认 https://www.skyxcloud.cc
    PANEL_URL   面板地址（可选，多个候选用逗号分隔）
    PANEL_CACHE 面板域名缓存文件，默认 .panel-cache
    TWOFA_CODE  两步验证码（账户开了两步验证时必需）
    MAX_ROUNDS  整体重试轮数，默认 5
    PANEL_TRIES 轮内尝试获取面板地址的次数，默认 8
    SCKEY       Server酱 key，可选
    TGBOT       Telegram bot token，可选
    TGUSERID    Telegram chat id，可选
"""

import json
import os
import random
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_BASE_URL = "https://www.skyxcloud.cc"
DEFAULT_CACHE = ".panel-cache"

# 人机验证页里提取这两个值即可通过 GoEdge WAF 滑动验证码
RE_CAPTCHA_ID = re.compile(r'name="GOEDGE_WAF_CAPTCHA_ID"\s+value="([^"]+)"')
RE_NONCE = re.compile(r'setAttribute\("value","(\d+)"\)')
# 落地页跳转表单里的 nonce
RE_PAGE_NONCE = re.compile(r'name="nonce"\s+value="([^"]+)"')


def log(msg):
    print(msg, flush=True)


class PanelCache:
    """面板地址缓存。存下已发现的域名，避免每次都去撞入口站的限流配额。

    **按站点分文件**：不同站点的面板地址互不相干，共用一个文件会串。
    `path` 当作目录用（默认 `.panel-cache`），里面按站点主机名分文件。
    """

    def __init__(self, path=DEFAULT_CACHE, site=None):
        self.dir = path
        self.site = site

    def _file(self):
        """缓存文件路径。给了 site 就按站点分开存。"""
        if not self.site:
            return self.dir
        safe = re.sub(r"[^A-Za-z0-9.-]+", "_", self.site)
        return os.path.join(self.dir, safe)

    def read(self):
        try:
            with open(self._file(), encoding="utf-8") as fh:
                data = fh.read().strip()
            return data if data.startswith("http") else None
        except OSError:
            return None

    def write(self, url):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._file(), "w", encoding="utf-8") as fh:
                fh.write(url)
            log(f"  已缓存面板地址 -> {self._file()}")
        except OSError as exc:
            log(f"  缓存写入失败（不影响本次签到）：{exc}")


class CheckinClient:
    """一个站点的签到客户端。

    站点可以是「直连型」（当前域名就是面板）或「跳转型」（要过验证再跳）。
    两种都用同一套流程：探测当前域名能否直接登录，不能就过验证拿跳转目标。
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, panel_url=None, timeout=40,
                 candidates=None, retry_sleep=None, cache=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.entry = self._new_session()
        self.panel = self._new_session()
        self.panel_url = panel_url.rstrip("/") if panel_url else None
        self.candidates = candidates if candidates is not None else []
        # 退避基数，测试里可以调小
        self.retry_sleep = retry_sleep if retry_sleep is not None else 3
        self.cache = cache if cache is not None else PanelCache()
        # 过验证后拿到的跳转目标，等真正确认它是面板后才算
        self._pending = None

    @staticmethod
    def _new_session():
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        return session

    def _session_for(self, url):
        """按域名挑 session：面板域名用 panel，其余用 entry。"""
        host = urlparse(url).hostname or ""
        entry_host = urlparse(self.base_url).hostname or ""
        if host == entry_host or not self.panel_url:
            return self.entry
        return self.panel

    # ---------- 低层请求 ----------
    def _request(self, method, url, tag, **kwargs):
        """带重试的请求。

        站点会对高频请求回 429 或直接断连，退避后重试即可。
        """
        last = None
        backoff = self.retry_sleep
        for attempt in range(5):
            session = self._session_for(url)
            try:
                resp = session.request(
                    method, url, timeout=self.timeout, allow_redirects=False, **kwargs
                )
                if resp.status_code in (429, 502, 503, 504):
                    log(f"  [{tag}] {method} {url} -> {resp.status_code}，{backoff}s 后重试")
                    last = resp
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                log(f"  [{tag}] {method} {url} -> {resp.status_code}")
                return resp
            except requests.RequestException as exc:
                last = exc
                log(f"  [{tag}] 连接失败（第 {attempt + 1} 次）：{exc}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        if isinstance(last, requests.Response):
            return last
        raise last

    # ---------- 面板地址探测 ----------
    def _looks_like_panel(self, url):
        """探测某个地址是不是面板：/auth/login 能出登录表单就算。"""
        try:
            resp = self._request("GET", url + "/auth/login", "GET login")
        except requests.RequestException as exc:
            log(f"    {url} 不可用：{exc}")
            return False
        if resp.status_code == 200 and "login_form" in resp.text:
            return True
        log(f"    {url} 不是面板（{resp.status_code}）")
        return False

    def _adopt(self, url):
        """确认某地址是面板，设为当前面板并缓存。"""
        self.panel_url = url.rstrip("/")
        self.cache.write(self.panel_url)
        return True

    # ---------- 第 1 步：确定面板地址 ----------
    def resolve_panel(self):
        """确定面板地址，按「稳 → 不稳」逐个尝试。

        直连型站点在这里第 3 步就命中；跳转型站点要走到第 4 步。
        """
        # 1. 显式配置
        if self.panel_url:
            log(f"  面板地址（配置）: {self.panel_url}")
            if self._looks_like_panel(self.panel_url):
                return self._adopt(self.panel_url)
            log("  配置的地址不可用，继续找别的")

        # 2. 上次缓存的
        cached = self.cache.read()
        if cached:
            log(f"  面板地址（缓存）: {cached}")
            if self._looks_like_panel(cached):
                return self._adopt(cached)
            log("  缓存的地址已失效，重新发现")

        # 3. 当前域名本身（直连型站点走这里）
        log(f"  探测当前域名是否直接是面板: {self.base_url}")
        if self._looks_like_panel(self.base_url):
            return self._adopt(self.base_url)

        # 4. 其它候选
        for candidate in self.candidates:
            log(f"  试候选面板地址: {candidate}")
            if self._looks_like_panel(candidate):
                return self._adopt(candidate)

        # 5. 过人机验证 + 跟随跳转（跳转型站点走这里）
        log("  走人机验证与跳转流程...")
        return self.discover_via_waf()

    # ---------- 第 2 步：过人机验证 ----------
    def pass_waf(self):
        """访问站点，必要时通过人机验证。

        支持两类：

        - **GoEdge WAF 滑动验证码**：页面里只有 `GOEDGE_WAF_CAPTCHA_ID` 隐藏字段
          与写死在 JS 里的 `nonce`，服务端只比对这两个值、不做轨迹校验，
          所以纯 requests 就能过。

        - **Cloudflare 挑战**（Turnstile 等）：服务端会校验浏览器指纹与
          JS 计算结果，纯 HTTP 过不了。这种情况脚本会明确报出来，
          需要改用真实浏览器（见仓库里的 DS-beta.py 思路）。
        """
        for _ in range(5):
            resp = self._request("GET", self.base_url + "/", "GET /")

            # Cloudflare 挑战：识别出来就直接说明，不浪费后续尝试
            if self._is_cloudflare_challenge(resp):
                log("  人机验证: 遇到 Cloudflare 挑战（需真实浏览器，纯 HTTP 过不了）")
                log("            可以改用 PANEL_URL 指向已通过验证的面板地址")
                return False

            location = resp.headers.get("Location")
            if not location:
                log("  人机验证: 直接放行")
                return True

            # 其它站点可能用别的跳转路径，只要 Location 里有验证码特征就继续处理
            if "captcha" not in location.lower() and "verify" not in location.lower():
                log(f"  人机验证: 未知跳转 {location[:70]}")
                return False

            log(f"  人机验证: 需要滑动验证 -> {location[:70]}")
            captcha_url = urljoin(self.base_url + "/", location)
            page = self._request("GET", captcha_url, "GET captcha").text

            captcha_id = RE_CAPTCHA_ID.search(page)
            nonce = RE_NONCE.search(page)
            if not captcha_id or not nonce:
                log("  人机验证: 页面结构变了，无法解析验证参数")
                return False

            resp = self._request(
                "POST",
                captcha_url,
                "POST captcha",
                headers={
                    "Referer": captcha_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.base_url,
                },
                data={
                    "GOEDGE_WAF_CAPTCHA_ID": captcha_id.group(1),
                    "nonce": nonce.group(1),
                },
            )
            if resp.status_code in (302, 303):
                log("  人机验证: 滑动验证通过")
                return True
            log(f"  人机验证: 未通过（{resp.status_code}），重试")
        return False

    @staticmethod
    def _is_cloudflare_challenge(resp):
        """判断响应是不是 Cloudflare 的人机挑战。"""
        if resp.headers.get("cf-mitigated") == "challenge":
            return True
        server = (resp.headers.get("server") or "").lower()
        if "cloudflare" in server and resp.status_code in (403, 503):
            return True
        try:
            body = resp.text[:4000].lower()
        except Exception:
            return False
        return "just a moment" in body or "cf_chl_opt" in body or "challenge-platform" in body

    # ---------- 第 3 步：过验证后找面板 ----------
    def discover_via_waf(self, tries=None):
        """过完人机验证后，找出真正的面板地址。

        两种可能：
          - 响应里直接 302/303 跳到别的域名 → 那是面板
          - 停在落地页，有个 continue 表单 → 提交后跳到面板

        continue 接口有每 IP 配额，实测大约 40% 成功率、间隔不规则，
        所以这里在轮内多试几次并随机间隔，比只试一次靠谱。
        """
        if tries is None:
            tries = int(os.environ.get("PANEL_TRIES", "8"))
        if not self.pass_waf():
            log("  人机验证未通过")
            return False

        for attempt in range(1, tries + 1):
            # 先看有没有直接跳转
            resp = self._request("GET", self.base_url + "/", "GET landing")
            location = resp.headers.get("Location")
            if location and resp.status_code in (301, 302, 303, 307):
                target = urljoin(self.base_url + "/", location).rstrip("/")
                log(f"  跳转目标: {target}")
                if self._looks_like_panel(target):
                    return self._adopt(target)
                log("  跳转目标不是面板")

            # 落地页的 continue 表单
            nonce = RE_PAGE_NONCE.search(resp.text)
            if not nonce:
                log("  落地页: 没找到 nonce，也没找到跳转")
                return False

            resp = self._request(
                "POST",
                self.base_url + "/",
                "POST continue",
                headers={
                    "Referer": self.base_url + "/",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.base_url,
                },
                data={"action": "continue", "nonce": nonce.group(1)},
            )
            if resp.status_code == 429:
                log(f"  落地页: continue 被限流（429），第 {attempt}/{tries} 次")
            elif not resp.headers.get("Location"):
                log(f"  落地页: 提交被拒（{resp.status_code}），第 {attempt}/{tries} 次")
            else:
                target = urljoin(self.base_url + "/", resp.headers["Location"]).rstrip("/")
                log(f"  面板地址（表单跳转）: {target}")
                if self._looks_like_panel(target):
                    return self._adopt(target)
                log("  跳转目标不是面板")

            if attempt < tries:
                # 配额是抢占式的，随机间隔更容易撞上空窗
                wait = random.uniform(2, 12)
                log(f"  {wait:.0f}s 后再试")
                time.sleep(wait)
        return False

    # ---------- 登录 ----------
    def login(self, email, password, twofa_code=""):
        url = self.panel_url + "/auth/login"
        resp = self._request(
            "POST",
            url,
            "POST login",
            headers={
                "Referer": url,
                "Origin": self.panel_url,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            data={
                "email": email,
                "passwd": password,
                "code": twofa_code or "",
                "remember_me": "on",
            },
        )
        try:
            result = resp.json()
        except ValueError:
            log(f"  登录: 返回不是 JSON（{resp.status_code}）")
            return False, None

        ret = result.get("ret")
        msg = result.get("msg", "")
        if ret == 1:
            log(f"  登录: 成功（{msg}）")
            return True, msg
        if ret == 2:
            log("  登录: 账户开启了两步验证，需要 TWOFA_CODE")
            return False, msg
        log(f"  登录: 失败（{msg or resp.status_code}）")
        return False, msg

    # ---------- 签到 ----------
    def checkin(self):
        url = self.panel_url + "/user/checkin"
        resp = self._request(
            "POST",
            url,
            "POST checkin",
            headers={
                "Referer": self.panel_url + "/user",
                "Origin": self.panel_url,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            data={},
        )
        try:
            return resp.json()
        except ValueError:
            log(f"  签到: 返回不是 JSON（{resp.status_code}）")
            return {"ret": 0, "msg": f"HTTP {resp.status_code}"}


def is_already_checked(result):
    """判断接口返回的是不是「今天已经签到过了」。

    站点对重复签到不返回 ret=1，而是 ret=0 + 一句提示（如"您似乎已经签到过了"）。
    这种情况说明今天流量已到账，属于成功，不该当失败处理。
    """
    msg = str(result.get("msg") or "")
    markers = ("已经签到", "已签到", "签到过", "重复签到", "already")
    return any(m in msg.lower() if m == "already" else m in msg for m in markers)


def notify(result):
    """推送签到结果。Server酱 与 Telegram 各一条，失败只记录不中断。"""
    msg = str(result.get("msg") or "")
    traffic = result.get("traffic")
    summary = f"机场签到：{msg}"
    if traffic:
        summary += f"（剩余流量 {traffic}）"

    sckey = os.environ.get("SCKEY", "")
    if sckey:
        try:
            resp = requests.get(
                f"https://sctapi.ftqq.com/{sckey}.send",
                params={"title": "机场签到", "desp": summary},
                timeout=20,
            )
            log(f"  Server酱: {resp.status_code}")
        except requests.RequestException as exc:
            log(f"  Server酱: 推送失败 {exc}")

    tg_token = os.environ.get("TGBOT", "")
    tg_user = os.environ.get("TGUSERID", "")
    if tg_token and tg_user:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                params={
                    "chat_id": tg_user,
                    "text": summary,
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )
            log(f"  Telegram: {resp.status_code}")
        except requests.RequestException as exc:
            log(f"  Telegram: 推送失败 {exc}")


def main():
    email = os.environ.get("EMAIL", "")
    password = os.environ.get("PASSWORD", "")
    base_url = os.environ.get("BASE_URL", DEFAULT_BASE_URL)
    panel_url = os.environ.get("PANEL_URL", "")
    cache_path = os.environ.get("PANEL_CACHE", DEFAULT_CACHE)
    twofa_code = os.environ.get("TWOFA_CODE", "")
    rounds = int(os.environ.get("MAX_ROUNDS", "5"))

    if not email or not password:
        log("必须设置 EMAIL 与 PASSWORD 环境变量")
        return 1

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    candidates = []
    for raw in panel_url.split(","):
        u = raw.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        candidates.append(u.rstrip("/"))

    log(f"站点: {base_url}")
    if candidates:
        log(f"面板候选: {', '.join(candidates)}")
    log(f"面板缓存文件: {cache_path}")

    # 入口站的 continue 有每 IP 配额，GitHub Actions 的共享 runner IP 常被占满。
    # 整个流程失败时退避重试，等配额窗口过去还有机会。
    for attempt in range(1, rounds + 1):
        if attempt > 1:
            # 配额是抢占式的，随机间隔比固定间隔更容易撞上空窗
            wait = random.uniform(20, 90)
            log(f"第 {attempt}/{rounds} 轮重试，{wait:.0f}s 后开始")
            time.sleep(wait)

        client = CheckinClient(
            base_url,
            panel_url=None,
            candidates=candidates,
            cache=PanelCache(cache_path, site=urlparse(base_url).hostname or base_url),
        )
        try:
            if not client.resolve_panel():
                log(f"第 {attempt}/{rounds} 轮: 找不到可用的面板地址")
                continue

            ok, _ = client.login(email, password, twofa_code)
            if not ok:
                log(f"第 {attempt}/{rounds} 轮: 登录失败")
                return 1

            result = client.checkin()
        except requests.RequestException as exc:
            log(f"第 {attempt}/{rounds} 轮: 网络异常 {exc}")
            continue

        log(f"签到结果: {result.get('msg')}")
        if result.get("trafficInfo"):
            log(f"流量信息: {json.dumps(result['trafficInfo'], ensure_ascii=False)}")

        notify(result)
        # 「已经签到过」不算失败——说明今天的量已经到了，重复跑只会得到这句。
        # 其它情况按接口的 ret 判断。
        if result.get("ret") == 1 or is_already_checked(result):
            return 0
        return 1

    log("所有轮次都失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
