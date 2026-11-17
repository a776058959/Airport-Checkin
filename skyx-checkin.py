#!/usr/bin/env python3
"""SKY VPN (skyxcloud.cc / skyvpn.bond) 自动签到。

站点结构（2026-11 实测）：

    https://www.skyxcloud.cc/            入口站，GoEdge WAF 保护
      └─ 307 → /WAF/VERIFY/CAPTCHA      GoEdge 滑动验证码（弱校验）
      └─ 落地页 form(action=continue, nonce)
      └─ 303 → https://<panel>.skyvpn.bond   真正的 SSPANEL 面板
           └─ POST /auth/login           返回 {ret, msg}
           └─ POST /user/checkin         返回 {ret, msg, traffic, trafficInfo}

三条关键事实（决定了本脚本的结构）：

1. **GoEdge WAF 的滑动验证码是弱校验**：页面里只有 `GOEDGE_WAF_CAPTCHA_ID`
   隐藏字段与写死在 JS 里的 `nonce`，服务端只比对这两个值，不做轨迹校验。
   因此纯 requests 就能过，不需要浏览器自动化。

2. **面板域名是随机分配的**（形如 `q210ycweb.skyvpn.bond`、`subb3xq6b.skyvpn.bond`），
   唯一获取途径是入口站落地页的 continue 表单。

3. **入口站 continue 有每 IP 配额**：实测连续调 3 次，第 3 次起返回 429，
   且 429 会连带影响后续落地页访问。GitHub Actions 的 runner IP 共享且常被
   占满，所以不能每次都走入口站。

对策是**缓存面板域名**：本地缓存文件（Actions 用 actions/cache 跨 run 持久化）
存下已发现的域名，下次直接用；只有缓存失效时才回入口站重新发现。

用法：
    EMAIL=... PASSWORD=... python3 skyx-checkin.py

环境变量：
    EMAIL       登录邮箱（必需）
    PASSWORD    登录密码（必需）
    PANEL_URL   面板站地址（可选，多个候选用逗号分隔；优先于缓存）
    PANEL_CACHE 面板域名缓存文件路径，默认 .panel-cache
    BASE_URL    入口站地址，默认 https://www.skyxcloud.cc
    TWOFA_CODE  两步验证码（账户开了两步验证时必需）
    SCKEY       Server酱 key，可选
    TGBOT       Telegram bot token，可选
    TGUSERID    Telegram chat id，可选
"""

import json
import os
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


def log(msg):
    print(msg, flush=True)


class PanelCache:
    """面板域名缓存。存下已发现的域名，避免每次都去撞入口站的限流配额。"""

    def __init__(self, path=DEFAULT_CACHE):
        self.path = path

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = fh.read().strip()
            return data if data.startswith("http") else None
        except OSError:
            return None

    def write(self, url):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(url)
            log(f"  已缓存面板域名 -> {self.path}")
        except OSError as exc:
            log(f"  缓存写入失败（不影响本次签到）：{exc}")


class SkyXClient:
    """按 面板站 -> 登录 -> 签到 的顺序跑链路。

    入口站与面板站是两个不同域名，各用一个 Session，cookie 不互相污染
    （否则面板站会收到入口站的 PHPSESSID）。
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

    # ---------- 第 1 步：确定面板站地址 ----------
    def resolve_panel(self):
        """确定面板站地址。

        面板域名随机分配，唯一来源是入口站，但入口站 continue 有每 IP 配额，
        所以按「稳 → 不稳」排序，逐个尝试：

          1. 显式配置的 PANEL_URL
          2. 上次缓存的域名（存在缓存文件里）
          3. 其它候选域名
          4. 入口站落地页跳转（消耗配额，最后手段）
        """
        if self.panel_url:
            log(f"  面板站（配置）: {self.panel_url}")
            if self._check_panel(self.panel_url):
                return True
            log("  配置的面板地址不可用，继续找别的")

        cached = self.cache.read()
        if cached:
            log(f"  面板站（缓存）: {cached}")
            if self._check_panel(cached):
                return True
            log("  缓存的面板地址已失效，重新发现")

        for candidate in self.candidates:
            log(f"  试候选面板域名: {candidate}")
            if self._check_panel(candidate):
                return True

        log("  其它候选都不可用，走入口站发现（会消耗限流配额）...")
        return self.enter_panel()

    def _check_panel(self, url):
        """探活：面板站 /auth/login 能出登录表单就算可用。"""
        try:
            resp = self._request("GET", url + "/auth/login", "GET panel")
        except requests.RequestException as exc:
            log(f"    不可用：{exc}")
            return False
        if resp.status_code == 200 and "login_form" in resp.text:
            self.panel_url = url
            self.cache.write(url)
            return True
        log(f"    不可用（{resp.status_code}）")
        return False

    # ---------- 第 2 步：过 GoEdge WAF 滑动验证码 ----------
    def pass_waf(self):
        """访问入口站，必要时通过 WAF 滑动验证码。

        滑动验证码页面里有两个值：
          - GOEDGE_WAF_CAPTCHA_ID：隐藏表单字段
          - nonce：页面 JS 写死的数字
        把它们 POST 回验证地址，服务端就发放 ge_wc_* cookie。
        """
        for _ in range(5):
            resp = self._request("GET", self.base_url + "/", "GET /")
            location = resp.headers.get("Location")
            if not location:
                log("  WAF: 直接放行")
                return True

            log(f"  WAF: 需要滑动验证 -> {location[:70]}")
            captcha_url = urljoin(self.base_url + "/", location)
            page = self._request("GET", captcha_url, "GET captcha").text

            captcha_id = re.search(
                r'name="GOEDGE_WAF_CAPTCHA_ID"\s+value="([^"]+)"', page
            )
            nonce = re.search(r'setAttribute\("value","(\d+)"\)', page)
            if not captcha_id or not nonce:
                log("  WAF: 页面结构变了，无法解析验证参数")
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
                log("  WAF: 滑动验证通过")
                return True
            log(f"  WAF: 验证未通过（{resp.status_code}），重试")
        return False

    # ---------- 第 3 步：落地页 -> 面板站 ----------
    def enter_panel(self):
        """入口站落地页有个 continue 表单，提交后 303 到真正的面板域名。"""
        if not self.pass_waf():
            log("  入口站: WAF 未通过")
            return False

        page = self._request("GET", self.base_url + "/", "GET landing").text
        nonce = re.search(r'name="nonce"\s+value="([^"]+)"', page)
        if not nonce:
            log("  落地页: 没找到 nonce")
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
        if resp.status_code in (429, 403) or not resp.headers.get("Location"):
            if resp.status_code == 429:
                log("  落地页: continue 被限流（429），本 IP 的入口站配额已用完")
                log("            —— 面板域名来自入口站，配额用完就拿不到；"
                    "缓存命中时不会走到这里")
            else:
                log(f"  落地页: 提交被拒（{resp.status_code}），入口站这条路走不通")
            return False

        location = resp.headers.get("Location")
        panel = urljoin(self.base_url + "/", location).rstrip("/")
        log(f"  面板站（入口站跳转）: {panel}")
        if self._check_panel(panel):
            return True
        return False

    # ---------- 第 4 步：登录 ----------
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

    # ---------- 第 5 步：签到 ----------
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

    if not email or not password:
        log("必须设置 EMAIL 与 PASSWORD 环境变量")
        return 1

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    # PANEL_URL 可以给多个候选（逗号分隔），按顺序探活
    candidates = []
    for raw in panel_url.split(","):
        u = raw.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        candidates.append(u.rstrip("/"))

    log(f"入口站: {base_url}")
    if candidates:
        log(f"面板候选: {', '.join(candidates)}")
    log(f"面板缓存文件: {cache_path}")

    # 入口站 continue 有每 IP 配额，GitHub Actions 的共享 runner IP 常被占满。
    # 整个流程失败时退避重试，等配额窗口过去还有机会。
    rounds = int(os.environ.get("MAX_ROUNDS", "3"))
    for attempt in range(1, rounds + 1):
        if attempt > 1:
            wait = min(30 * (2 ** (attempt - 2)), 300)
            log(f"第 {attempt}/{rounds} 轮重试，{wait}s 后开始")
            time.sleep(wait)

        client = SkyXClient(
            base_url,
            panel_url=None,
            candidates=candidates,
            cache=PanelCache(cache_path),
        )
        try:
            if not client.resolve_panel():
                log(f"第 {attempt}/{rounds} 轮: 无法确定面板站地址")
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
        return 0 if result.get("ret") == 1 else 1

    log("所有轮次都失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
