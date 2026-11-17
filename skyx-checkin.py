#!/usr/bin/env python3
"""SKY VPN (skyxcloud.cc) 自动签到。

站点结构（2026-11 实测）：

    https://www.skyxcloud.cc/            入口站，GoEdge WAF 保护
      └─ 307 → /WAF/VERIFY/CAPTCHA      GoEdge 滑动验证码（弱校验）
      └─ 落地页 form(action=continue, nonce)
      └─ 303 → https://<panel>.skyvpn.bond   真正的 SSPANEL 面板
           └─ POST /auth/login           返回 {ret, msg}
           └─ POST /user/checkin         返回 {ret, msg, traffic, trafficInfo}

GoEdge WAF 的滑动验证码只校验两个表单字段（GOEDGE_WAF_CAPTCHA_ID 与
页面 JS 里写死的 nonce），服务端不做轨迹校验，因此不需要真实浏览器，
纯 requests 即可通过。

用法：
    EMAIL=... PASSWORD=... BASE_URL=https://www.skyxcloud.cc python3 skyx-checkin.py

环境变量：
    EMAIL       登录邮箱（必需）
    PASSWORD    登录密码（必需）
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


def log(msg):
    print(msg, flush=True)


class SkyXClient:
    """按入口站 -> 面板站的顺序跑完整条链路。

    入口站与面板站是两个不同域名，各用一个 Session，cookie 不互相污染
    （否则面板站会收到入口站的 PHPSESSID）。
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=40):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.entry = self._new_session()
        self.panel = self._new_session()
        self.panel_url = None

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
        """带重试的请求。目标站偶发连接超时，重试即可。"""
        session = self._session_for(url)
        last = None
        for attempt in range(4):
            try:
                resp = session.request(
                    method, url, timeout=self.timeout, allow_redirects=False, **kwargs
                )
                log(f"  [{tag}] {method} {url} -> {resp.status_code}")
                return resp
            except requests.RequestException as exc:
                last = exc
                log(f"  [{tag}] 连接失败（第 {attempt + 1} 次）：{exc}")
                time.sleep(2)
        raise last

    # ---------- 第 1 步：过 GoEdge WAF 滑动验证码 ----------
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

    # ---------- 第 2 步：落地页 -> 面板站 ----------
    def enter_panel(self):
        """入口站落地页有个 continue 表单，提交后 303 到真正的面板域名。"""
        page = self._request("GET", self.base_url + "/", "GET landing").text
        nonce = re.search(r'name="nonce"\s+value="([^"]+)"', page)
        if not nonce:
            log("  落地页: 没找到 nonce，可能已直达面板")
            self.panel_url = self.base_url
            return self.panel_url

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
        location = resp.headers.get("Location")
        if not location:
            log("  落地页: 没有跳转目标")
            self.panel_url = self.base_url
            return self.panel_url

        self.panel_url = urljoin(self.base_url + "/", location).rstrip("/")
        log(f"  面板站: {self.panel_url}")
        # 访问一次面板登录页，让面板站自己下发 server_name_session cookie
        self._request("GET", self.panel_url + "/auth/login", "GET panel")
        return self.panel_url

    # ---------- 第 3 步：登录 ----------
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

    # ---------- 第 4 步：签到 ----------
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
    twofa_code = os.environ.get("TWOFA_CODE", "")

    if not email or not password:
        log("必须设置 EMAIL 与 PASSWORD 环境变量")
        return 1

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    log(f"入口站: {base_url}")

    client = SkyXClient(base_url)
    try:
        if not client.pass_waf():
            log("WAF 验证未通过")
            return 1

        client.enter_panel()

        ok, _ = client.login(email, password, twofa_code)
        if not ok:
            return 1

        result = client.checkin()
    except requests.RequestException as exc:
        log(f"网络异常：{exc}")
        return 1

    log(f"签到结果: {result.get('msg')}")
    if result.get("trafficInfo"):
        log(f"流量信息: {json.dumps(result['trafficInfo'], ensure_ascii=False)}")

    notify(result)
    return 0 if result.get("ret") == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
