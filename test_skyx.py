"""用本地 mock 服务器验证 pass_waf 的完整分支逻辑。

mock 复刻真实站点的行为：
  GET /                       -> 307 到 /WAF/VERIFY/CAPTCHA?info=...&from=%2F
  GET /WAF/VERIFY/CAPTCHA     -> 200 返回含 GOEDGE_WAF_CAPTCHA_ID 与写死 nonce 的页面
  POST /WAF/VERIFY/CAPTCHA    -> 校验两个字段，通过则 303 + 下发 ge_wc_* cookie
  GET /                       -> 200 落地页
  POST / (action=continue)    -> 303 跳到面板
  GET 面板 /auth/login        -> 200 返回登录页
  POST 面板 /auth/login       -> 200 返回 {"ret":0,"msg":"邮箱不存在"}（模拟错误密码）
  POST 面板 /user/checkin     -> 200 返回 {"ret":1,"msg":"获得了 16MB 流量."}
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

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

STATE = {"waf_passed": False, "logged_in": False}


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

    def _cookies(self):
        return self.headers.get("Cookie") or ""

    def do_GET(self):
        path = urlparse(self.path).path
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
        elif path == "/panel/auth/login":
            self._send(200, b"<html><title>login</title></html>", {"Content-Type": "text/html"})
        else:
            self._send(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        path = urlparse(self.path).path
        fields = parse_qs(body)

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
            self._send(303, headers={"Location": "http://localhost:%d/panel" % PORT})
        elif path == "/panel/auth/login":
            # 模拟：密码正确才 ret=1
            if fields.get("passwd", [""])[0] == "goodpass":
                STATE["logged_in"] = True
                self._send(200, json.dumps({"ret": 1, "msg": "登录成功"}).encode(),
                           {"Content-Type": "application/json"})
            else:
                self._send(200, json.dumps({"ret": 0, "msg": "密码错误"}).encode(),
                           {"Content-Type": "application/json"})
        elif path == "/panel/user/checkin":
            if not STATE["logged_in"]:
                self._send(302, headers={"Location": "/panel/auth/login"})
            else:
                self._send(200, json.dumps({
                    "ret": 1,
                    "msg": "获得了 16MB 流量.",
                    "traffic": "10.02GB",
                    "trafficInfo": {"todayUsedTraffic": "0B", "lastUsedTraffic": "0B",
                                    "unUsedTraffic": "10.02GB"},
                }).encode(), {"Content-Type": "application/json"})
        else:
            self._send(404, b"not found")


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    print(f"mock server on 127.0.0.1:{PORT}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import importlib.util
    spec = importlib.util.spec_from_file_location("skyx", "skyx-checkin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    base = f"http://127.0.0.1:{PORT}"
    client = mod.SkyXClient(base)

    print("\n--- 1) pass_waf ---")
    assert client.pass_waf() is True, "pass_waf 失败"
    assert STATE["waf_passed"], "WAF 状态未置位"
    assert "ge_wc_91" in client.entry.cookies.get_dict(), "未拿到 ge_wc_91 cookie"
    print("  断言通过：WAF 通过 + cookie 下发")

    print("\n--- 2) enter_panel ---")
    panel = client.enter_panel()
    assert panel.endswith("/panel"), f"面板地址不对: {panel}"
    print(f"  断言通过：面板地址 {panel}")

    print("\n--- 3) 登录失败分支 ---")
    ok, msg = client.login("a@b.com", "wrongpass")
    assert ok is False and msg == "密码错误", f"登录失败分支不对: {ok}, {msg}"
    print(f"  断言通过：ret=0 时返回 False，msg={msg}")

    print("\n--- 4) 登录成功分支 ---")
    ok, msg = client.login("a@b.com", "goodpass")
    assert ok is True, "登录成功分支不对"
    print(f"  断言通过：ret=1 时返回 True，msg={msg}")

    print("\n--- 5) 签到 ---")
    result = client.checkin()
    assert result.get("ret") == 1, f"签到结果不对: {result}"
    assert result.get("traffic") == "10.02GB", f"流量字段不对: {result}"
    print(f"  断言通过：ret=1，msg={result['msg']}，traffic={result['traffic']}")

    print("\n--- 6) cookie 域隔离 ---")
    # 入口站的 PHPSESSID 不能出现在面板请求里
    assert client.entry is not client.panel, "入口站与面板站必须用不同 Session"
    entry_cookies = set(client.entry.cookies.get_dict())
    panel_cookies = set(client.panel.cookies.get_dict())
    print(f"  entry cookies : {sorted(entry_cookies)}")
    print(f"  panel cookies : {sorted(panel_cookies)}")
    # 面板站请求用的确实是 panel session
    from urllib.parse import urlparse
    assert client._session_for(client.base_url + "/") is client.entry
    assert client._session_for(client.panel_url + "/auth/login") is client.panel
    print("  断言通过：按域名选对了 Session")

    print("\n全部断言通过")
