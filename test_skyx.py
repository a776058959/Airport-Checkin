"""用本地 mock 服务器验证 skyx-checkin.py 的完整分支逻辑。

脚本是通用的：先探测当前域名是不是面板，不是才走人机验证+跳转。
mock 复刻两种站点形态：

  跳转型（入口站只是中转）：
    GET  /                      -> 307 到 /WAF/VERIFY/CAPTCHA
    GET  /WAF/VERIFY/CAPTCHA    -> 200 含 GOEDGE_WAF_CAPTCHA_ID 与写死 nonce
    POST /WAF/VERIFY/CAPTCHA    -> 校验两个字段，通过则 303 + 放行 cookie
    GET  /                      -> 200 落地页（form action=continue）
    POST / (action=continue)    -> 303 跳到面板域名

  直连型（当前域名就是面板）：
    GET  /auth/login            -> 200 登录页（含 login_form）

覆盖场景：
  1. 跳转型完整链路
  2. 直连型：当前域名直接就是面板，不碰人机验证
  3. 登录成功/失败分支
  4. 签到
  5. 入口站 continue 返回 429 时正确失败
  6. cookie 域隔离
  7. 429 退避重试
  8. 面板地址缓存（写入 / 命中 / 失效后回落）
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CAPTCHA_ID = "testcaptchaid1234567890abcdef"
NONCE = "6945978132522097671"
CAPTCHA_HTML = """<!DOCTYPE html><html><head><title>Verify Yourself</title>
<script type="text/javascript">
var isValidated=!1;window.addEventListener("pageshow",function(){isValidated&&window.location.reload()}),window.addEventListener("load",function(){var n=document.getElementById("input"),s=document.getElementById("handler");var t=n.offsetLeft,u=s.offsetLeft,c=n.offsetWidth-s.offsetWidth-s.offsetLeft;function e(e){e.preventDefault()}function l(e){var t=(t=n.offsetLeft);s.offsetLeft<c-4?(s.style.cssText="margin-left: "+u+"px"):(s.style.cssText="margin-left: "+c+"px",n.style.cssText="background: #a5dc86",isValidated=!0,(t=document.createElement("input")).setAttribute("name","nonce"),t.setAttribute("type","hidden"),t.setAttribute("value","%s"),document.getElementById("ui-form").appendChild(t),document.getElementById("ui-form").submit())}});
</script></head><body><form method="POST" id="ui-form">
<input type="hidden" name="GOEDGE_WAF_CAPTCHA_ID" value="%s"/>
<div class="ui-input" id="input"><div class="ui-progress-bar" id="progress-bar"></div><div class="ui-handler" id="handler"></div></div>
<p class="ui-prompt">Slide to Unlock</p></form><address>Request ID: 123</address></body></html>""" % (
    NONCE,
    CAPTCHA_ID,
)

LANDING_HTML = """<!DOCTYPE html><html><body><form method="post" action="">
<input type="hidden" name="action" value="continue">
<input type="hidden" name="nonce" value="landingnonce1234567890">
<button type="submit">进入服务</button></form></body></html>"""

LOGIN_HTML = """<!DOCTYPE html><html><body><form id="login_form">
<input id="email" name="email"/><input id="password" name="passwd"/><input id="twofa_code" name="code"/>
</form></body></html>"""

STATE = {
    "waf_passed": False,
    "logged_in": False,
    "continue_mode": "ok",  # ok | 429
    # 直连型：当前域名自己就是面板
    "direct_panel": False,
    # Cloudflare 挑战
    "cloudflare": False,
    # continue 前 N 次返回 429，之后才成功
    "continue_fail_until": 0,
    "continue_calls": 0,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        host = self.headers.get("Host", "")
        on_panel = "localhost" in host

        if on_panel:
            if path.endswith("/auth/login"):
                self._send(200, LOGIN_HTML.encode(), {"Content-Type": "text/html"})
            else:
                self._send(404, b"not found")
            return

        if STATE["cloudflare"]:
            self._send(
                403,
                b'<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
                b'<body><script src="/cdn-cgi/challenge-platform/x"></script></body></html>',
                {"Content-Type": "text/html", "Server": "cloudflare",
                 "cf-mitigated": "challenge"},
            )
            return

        # 入口站
        if STATE["direct_panel"]:
            if path == "/auth/login":
                self._send(200, LOGIN_HTML.encode(), {"Content-Type": "text/html"})
            else:
                self._send(404, b"not found")
            return

        if path == "/":
            if not STATE["waf_passed"]:
                self._send(
                    307,
                    headers={"Location": "/WAF/VERIFY/CAPTCHA?info=abc%2Bdef&from=%2F"},
                )
            else:
                self._send(200, LANDING_HTML.encode(), {"Content-Type": "text/html"})
        elif path == "/WAF/VERIFY/CAPTCHA":
            self._send(200, CAPTCHA_HTML.encode(), {"Content-Type": "text/html"})
        else:
            self._send(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        path = urlparse(self.path).path
        host = self.headers.get("Host", "")
        on_panel = "localhost" in host
        fields = parse_qs(body)

        if on_panel or STATE["direct_panel"]:
            if path.endswith("/auth/login"):
                if fields.get("passwd", [""])[0] == "goodpass":
                    STATE["logged_in"] = True
                    self._send(
                        200,
                        json.dumps({"ret": 1, "msg": "登录成功"}).encode(),
                        {"Content-Type": "application/json"},
                    )
                else:
                    self._send(
                        200,
                        json.dumps({"ret": 0, "msg": "密码错误"}).encode(),
                        {"Content-Type": "application/json"},
                    )
            elif path.endswith("/user/checkin"):
                if not STATE["logged_in"]:
                    self._send(302, headers={"Location": "/auth/login"})
                else:
                    self._send(
                        200,
                        json.dumps(
                            {
                                "ret": 1,
                                "msg": "获得了 16MB 流量.",
                                "traffic": "10.02GB",
                                "trafficInfo": {
                                    "todayUsedTraffic": "0B",
                                    "lastUsedTraffic": "0B",
                                    "unUsedTraffic": "10.02GB",
                                },
                            }
                        ).encode(),
                        {"Content-Type": "application/json"},
                    )
            else:
                self._send(404, b"not found")
            return

        if path == "/WAF/VERIFY/CAPTCHA":
            got_id = (fields.get("GOEDGE_WAF_CAPTCHA_ID") or [""])[0]
            got_nonce = (fields.get("nonce") or [""])[0]
            if got_id == CAPTCHA_ID and got_nonce == NONCE:
                STATE["waf_passed"] = True
                self._send(
                    303,
                    headers={
                        "Location": "/",
                        "Set-Cookie": "ge_wc_91=testcookie==; Path=/; Max-Age=3600",
                    },
                )
            else:
                self._send(200, b"bad captcha")
        elif path == "/":
            STATE["continue_calls"] += 1
            if STATE["continue_mode"] == "429" or (
                STATE["continue_fail_until"]
                and STATE["continue_calls"] <= STATE["continue_fail_until"]
            ):
                self._send(429, b"too many requests")
            else:
                self._send(
                    303, headers={"Location": "http://localhost:%d/panel" % PORT}
                )
        else:
            self._send(404, b"not found")


def fresh_state():
    STATE.update(
        {
            "waf_passed": False,
            "logged_in": False,
            "continue_mode": "ok",
            "direct_panel": False,
            "cloudflare": False,
            "continue_fail_until": 0,
            "continue_calls": 0,
        }
    )


class TempCache:
    """测试用的假缓存：不落盘。"""

    def __init__(self, value=None):
        self.value = value
        self.written = None

    def read(self):
        return self.value

    def write(self, url):
        self.value = url
        self.written = url


def load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("skyx", "skyx-checkin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    print(f"mock server on 127.0.0.1:{PORT}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    mod = load_module()
    entry = f"http://127.0.0.1:{PORT}"
    panel = f"http://localhost:{PORT}/panel"

    def make(base=None, candidates=None, cache=None):
        return mod.CheckinClient(
            base or entry,
            candidates=candidates if candidates is not None else [],
            retry_sleep=0,
            cache=cache if cache is not None else TempCache(),
        )

    # ---------- 1) 跳转型：入口站发现面板 ----------
    print("\n=== 1) 跳转型完整链路 ===")
    fresh_state()
    client = make()
    assert client.resolve_panel() is True, "面板发现失败"
    assert client.panel_url == panel, f"面板地址不对: {client.panel_url}"
    assert STATE["waf_passed"], "WAF 未置位"
    assert "ge_wc_91" in client.entry.cookies.get_dict(), "入口站未拿到放行 cookie"
    print("  断言通过：过 WAF -> 落地页 -> 跳到面板")

    ok, msg = client.login("a@b.com", "wrongpass")
    assert ok is False and msg == "密码错误", f"登录失败分支不对: {ok}, {msg}"
    print("  断言通过：ret=0 返回 False")

    ok, msg = client.login("a@b.com", "goodpass")
    assert ok is True, "登录成功分支不对"
    print("  断言通过：ret=1 返回 True")

    result = client.checkin()
    assert result.get("ret") == 1 and result.get("traffic") == "10.02GB", f"签到结果不对: {result}"
    print("  断言通过：签到返回 ret=1 与 traffic")

    # ---------- 2) 直连型：当前域名就是面板 ----------
    print("\n=== 2) 直连型（当前域名直接是面板，不碰人机验证）===")
    fresh_state()
    STATE["direct_panel"] = True
    client2 = make()
    assert client2.resolve_panel() is True, "直连模式解析失败"
    assert client2.panel_url == entry, f"应直接用当前域名: {client2.panel_url}"
    assert STATE["waf_passed"] is False, "直连模式不该走 WAF"
    ok, _ = client2.login("a@b.com", "goodpass")
    assert ok is True, "直连模式登录失败"
    result = client2.checkin()
    assert result.get("ret") == 1, f"直连模式签到失败: {result}"
    print("  断言通过：全程未经过人机验证，登录与签到均成功")

    # ---------- 3) 入口站 continue 返回 429 ----------
    print("\n=== 3) 入口站 continue 返回 429 ===")
    fresh_state()
    STATE["continue_mode"] = "429"
    client3 = make()
    assert client3.resolve_panel() is False, "429 时不应判定成功"
    print("  断言通过：429 被识别为失败，不会拿空地址去登录")

    # ---------- 4) cookie 域隔离 ----------
    print("\n=== 4) cookie 域隔离 ===")
    fresh_state()
    client4 = make()
    client4.resolve_panel()
    assert client4.entry is not client4.panel, "入口站与面板站必须用不同 Session"
    assert client4._session_for(entry + "/") is client4.entry
    assert client4._session_for(panel + "/auth/login") is client4.panel
    print(f"  entry cookies : {sorted(client4.entry.cookies.get_dict())}")
    print(f"  panel cookies : {sorted(client4.panel.cookies.get_dict())}")
    print("  断言通过：按域名选对了 Session")

    # ---------- 5) 429 退避重试 ----------
    print("\n=== 5) 429 退避重试 ===")
    fresh_state()
    client5 = make()
    calls = {"n": 0}
    orig = client5._request

    def counting(method, url, tag, **kw):
        calls["n"] += 1
        return orig(method, url, tag, **kw)

    client5._request = counting
    STATE["continue_mode"] = "429"
    try:
        client5.resolve_panel()
    except Exception:
        pass
    assert calls["n"] > 1, f"429 应触发重试，实际调用 {calls['n']} 次"
    print(f"  断言通过：429 触发了重试（{calls['n']} 次请求）")

    # ---------- 6) 面板地址缓存 ----------
    print("\n=== 6) 面板地址缓存 ===")
    fresh_state()
    cache = TempCache()
    client6 = make(cache=cache)
    assert client6.resolve_panel() is True, "首次发现失败"
    assert cache.written == panel, f"未缓存面板地址: {cache.written}"
    print(f"  断言通过：发现后缓存了 {cache.written}")

    fresh_state()
    cache2 = TempCache(value=panel)
    client7 = make(cache=cache2)
    assert client7.resolve_panel() is True, "缓存命中时解析失败"
    assert STATE["waf_passed"] is False, "缓存命中时不该走 WAF"
    assert client7.panel_url == panel
    print("  断言通过：缓存命中时完全没碰入口站")

    fresh_state()
    cache3 = TempCache(value="http://localhost:1/dead")
    client8 = make(cache=cache3)
    assert client8.resolve_panel() is True, "缓存失效后应能重新发现"
    assert cache3.written == panel, f"失效后未更新缓存: {cache3.written}"
    print("  断言通过：缓存失效后回落入口站并更新缓存")

    # ---------- 7) 轮内多次尝试：前几次 429 之后能拿到面板 ----------
    print("\n=== 7) 轮内多次尝试拿面板（前 3 次 429）===")
    fresh_state()
    STATE["continue_fail_until"] = 3
    client10 = make()
    import os as _os
    _os.environ["PANEL_TRIES"] = "8"
    assert client10.resolve_panel() is True, "轮内重试后应能拿到面板"
    assert client10.panel_url == panel, f"面板地址不对: {client10.panel_url}"
    assert STATE["continue_calls"] >= 4, f"continue 调用次数不对: {STATE['continue_calls']}"
    print(f"  断言通过：continue 尝试 {STATE['continue_calls']} 次后拿到面板")

    # ---------- 8) Cloudflare 挑战应被识别并明确报错 ----------
    print("\n=== 7) Cloudflare 挑战识别 ===")
    fresh_state()
    STATE["cloudflare"] = True
    client9 = make()
    assert client9.resolve_panel() is False, "Cloudflare 挑战时不应判定成功"
    print("  断言通过：识别出 Cloudflare 挑战并明确失败")

    print("\n全部断言通过")
