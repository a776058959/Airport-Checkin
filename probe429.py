"""诊断入口站限流：实测 continue 接口的配额与恢复情况。

面板域名是随机分配的，唯一获取途径是入口站落地页的 continue 表单；
而该接口有每 IP 配额，用满会返回 429（再之后是空响应、直接断连）。
GitHub Actions 的 runner IP 共享且常被限流，本脚本用来确认配额消耗规律。

用法：
    python3 probe429.py              # 连续尝试，看配额耗尽
    python3 probe429.py 60           # 429 之后每 60s 试一次，看恢复窗口

注意：
  - 本机 DNS 若把 www.skyxcloud.cc 解析成 0.0.0.0，脚本会自动改用真实 IP
    并保留 Host/SNI（站点与面板同在 163.52.242.61）。
  - 这个脚本会真的消耗入口站的限流配额，跑多了本机 IP 会被临时拉黑。
"""

import re
import socket
import sys
import time

import requests

HOST = "www.skyxcloud.cc"
FALLBACK_IP = "163.52.242.61"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_orig = socket.getaddrinfo


def _resolve(host):
    """如果本机把域名解析成 0.0.0.0，就改用真实 IP（保留 Host 与 SNI）。"""
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(host, "A")
        ip = str(answers[0])
        if ip != "0.0.0.0":
            return ip, False
    except Exception:
        pass
    return FALLBACK_IP, True


REAL_IP, PATCHED = _resolve(HOST)
if PATCHED:
    print(f"注意：{HOST} 的 DNS 解析不到有效地址，改用 {REAL_IP}（Host/SNI 不变）")

    def _patched(host, port, *a, **k):
        if host == HOST:
            return _orig(REAL_IP, port, *a, **k)
        return _orig(host, port, *a, **k)

    socket.getaddrinfo = _patched

requests.packages.urllib3.disable_warnings()


def try_req(fn, *a, **k):
    """发请求，连接被断/重置都算一种结果而不是崩溃。"""
    try:
        return fn(*a, **k)
    except requests.RequestException as exc:
        return exc


def new_session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
    return s


def one_attempt():
    """全新 Session 走一遍，返回 continue 的状态码或错误标记。"""
    s = new_session()

    r = try_req(s.get, "https://" + HOST + "/", timeout=30, allow_redirects=False, verify=False)
    if isinstance(r, Exception):
        return "CONNRESET"

    if r.status_code == 307:
        # 需要过 WAF 滑动验证码
        loc = r.headers.get("Location")
        r2 = try_req(s.get, "https://" + HOST + loc, timeout=30, allow_redirects=False, verify=False)
        if isinstance(r2, Exception):
            return "CONNRESET"
        cid = re.search(r'name="GOEDGE_WAF_CAPTCHA_ID"\s+value="([^"]+)"', r2.text)
        nonce = re.search(r'setAttribute\("value","(\d+)"\)', r2.text)
        if not cid or not nonce:
            return "PARSE_FAIL"
        r3 = try_req(
            s.post, "https://" + HOST + loc,
            data={"GOEDGE_WAF_CAPTCHA_ID": cid.group(1), "nonce": nonce.group(1)},
            headers={"Referer": "https://" + HOST + loc, "Origin": "https://" + HOST},
            timeout=30, allow_redirects=False, verify=False,
        )
        if isinstance(r3, Exception):
            return "CONNRESET"
        if r3.status_code not in (302, 303):
            return f"WAF_FAIL({r3.status_code})"

    rp = try_req(s.get, "https://" + HOST + "/", timeout=30, allow_redirects=False, verify=False)
    if isinstance(rp, Exception):
        return "CONNRESET"
    n2 = re.search(r'name="nonce"\s+value="([^"]+)"', rp.text)
    if not n2:
        return f"NO_NONCE({rp.status_code})"

    rc = try_req(
        s.post, "https://" + HOST + "/",
        data={"action": "continue", "nonce": n2.group(1)},
        headers={"Referer": "https://" + HOST + "/", "Origin": "https://" + HOST},
        timeout=30, allow_redirects=False, verify=False,
    )
    if isinstance(rc, Exception):
        return "CONNRESET"
    if rc.status_code in (302, 303):
        return f"OK -> {rc.headers.get('Location')}"
    return rc.status_code


if __name__ == "__main__":
    wait = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    print("=== 连续尝试，看配额耗尽 ===")
    exhausted = False
    for i in range(5):
        code = one_attempt()
        print(f"  #{i+1}: {code}", flush=True)
        if code == 429:
            exhausted = True
            break
        time.sleep(2)

    if not exhausted:
        print("\n本次没打到限流（配额未被本机用尽）")
        sys.exit(0)

    print("\n=== 429 后的恢复窗口探测 ===")
    for w in (30, 60, 120, 180, 300):
        print(f"  等待 {w}s ...", flush=True)
        time.sleep(w)
        code = one_attempt()
        print(f"    -> {code}", flush=True)
        if isinstance(code, str) and code.startswith("OK"):
            print(f"  >>> 恢复：等待 {w}s 后重新可用")
            break
