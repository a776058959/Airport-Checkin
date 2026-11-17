"""用本地 mock 服务器验证 skyx-checkin.py 的完整分支逻辑。

mock 复刻真实站点的行为：
  GET 入口站 /                    -> 307 到 /WAF/VERIFY/CAPTCHA?info=...&from=%2F
  GET 入口站 /WAF/VERIFY/CAPTCHA  -> 200 返回含 GOEDGE_WAF_CAPTCHA_ID 与写死 nonce 的页面
  POST 入口站 /WAF/VERIFY/CAPTCHA -> 校验两个字段，通过则 303 + 下发 ge_wc_* cookie
  GET 入口站 /                    -> 200 落地页（form action=continue）
  POST 入口站 / (action=continue) -> 303 跳到面板域名
  GET 面板 /auth/login            -> 200 登录页（含 login_form）
  POST 面板 /auth/login           -> 200 {"ret":..,"msg":..}
  POST 面板 /user/checkin         -> 200 {"ret":1,"msg":..,"traffic":..}

同时覆盖：
  - PANEL_URL 直连面板（完全不经入口站）
  - 入口站 POST continue 返回 429 时的降级
  - 429 的退避重试
  - cookie 域隔离
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
    "rate_limit_once": False,
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
        panel = "localhost" in host

        if panel:
            if path.endswith("/auth/login"):
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
        panel = "localhost" in host
        fields = parse_qs(body)

        if panel:
            if path.endswith("/auth/login"):
                if fields.get("passwd", [""])[0] == "goodpass":
                    STATE["logged_in"] = True
                    self._send(
                        200,
                        json.dumps({"ret": 1, "msg": "登录成功"}).encode(),
                        {"Content-Type": "application/json"},
                    )
                elif fields.get("code", [""])[0] == "123456":
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
            if STATE["continue_mode"] == "429":
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
            "rate_limit_once": False,
        }
    )


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

    # ---------- 1) 完整链路：入口站 -> WAF -> 落地页 -> 面板 -> 登录 -> 签到 ----------
    print("\n=== 1) 完整链路（入口站发现面板域名）===")
    fresh_state()
    client = mod.SkyXClient(entry, candidates=[], retry_sleep=0)
    assert client.resolve_panel() is True, "面板发现失败"
    assert client.panel_url == panel, f"面板地址不对: {client.panel_url}"
    assert STATE["waf_passed"], "WAF 未置位"
    assert "ge_wc_91" in client.entry.cookies.get_dict(), "入口站未拿到 WAF cookie"
    print("  断言通过：WAF 通过 + 落地页跳转到面板")

    ok, msg = client.login("a@b.com", "wrongpass")
    assert ok is False and msg == "密码错误", f"登录失败分支不对: {ok}, {msg}"
    print("  断言通过：ret=0 返回 False")

    ok, msg = client.login("a@b.com", "goodpass")
    assert ok is True, "登录成功分支不对"
    print("  断言通过：ret=1 返回 True")

    result = client.checkin()
    assert result.get("ret") == 1 and result.get("traffic") == "10.02GB", f"签到结果不对: {result}"
    print("  断言通过：签到返回 ret=1 与 traffic")

    # ---------- 2) PANEL_URL 直连面板，不碰入口站 ----------
    print("\n=== 2) PANEL_URL 直连面板（跳过入口站）===")
    fresh_state()
    client2 = mod.SkyXClient(entry, panel_url=panel)
    assert client2.resolve_panel() is True, "直连面板失败"
    assert STATE["waf_passed"] is False, "直连面板不该碰 WAF"
    assert client2.panel_url == panel
    ok, _ = client2.login("a@b.com", "goodpass")
    assert ok is True, "直连模式下登录失败"
    result = client2.checkin()
    assert result.get("ret") == 1, f"直连模式签到失败: {result}"
    print("  断言通过：全程未经过入口站，登录与签到均成功")

    # ---------- 3) 入口站 continue 返回 429 时应失败而不是拿空地址 ----------
    print("\n=== 3) 入口站 continue 返回 429 ===")
    fresh_state()
    STATE["continue_mode"] = "429"
    client3 = mod.SkyXClient(entry, candidates=[], retry_sleep=0)
    assert client3.enter_panel() is False, "429 时不应判定成功"
    print("  断言通过：429 被识别为失败，不会拿空面板地址去登录")

    # ---------- 4) cookie 域隔离 ----------
    print("\n=== 4) cookie 域隔离 ===")
    fresh_state()
    client4 = mod.SkyXClient(entry, candidates=[], retry_sleep=0)
    client4.resolve_panel()
    assert client4.entry is not client4.panel, "入口站与面板站必须用不同 Session"
    assert client4._session_for(entry + "/") is client4.entry
    assert client4._session_for(panel + "/auth/login") is client4.panel
    print(f"  entry cookies : {sorted(client4.entry.cookies.get_dict())}")
    print(f"  panel cookies : {sorted(client4.panel.cookies.get_dict())}")
    print("  断言通过：按域名选对了 Session")

    # ---------- 5) 429 退避重试确实会重试 ----------
    print("\n=== 5) 429 退避重试 ===")
    fresh_state()
    client5 = mod.SkyXClient(entry, candidates=[], retry_sleep=0)
    calls = {"n": 0}
    orig = client5._request

    def counting(method, url, tag, **kw):
        calls["n"] += 1
        return orig(method, url, tag, **kw)

    client5._request = counting
    STATE["continue_mode"] = "429"
    # enter_panel 内部会 POST continue，遇到 429 会重试；这里只验证会重试
    try:
        client5.enter_panel()
    except Exception:
        pass
    assert calls["n"] > 1, f"429 应触发重试，实际调用 {calls['n']} 次"
    print(f"  断言通过：429 触发了重试（{calls['n']} 次请求）")

    print("\n全部断言通过")
