# 机场自动签到

自动签到脚本集合。原仓库面向 Xboard 类面板；本仓库新增了针对 **SKY VPN（GoEdge WAF + SSPANEL）** 的实现。

## SKY VPN 自动签到（skyx-checkin.py）

2026-11 重写。原来跑不通的原因不是 Cloudflare 人机验证变了，而是站点整体结构换了一套：

```
https://www.skyxcloud.cc/                     入口站（GoEdge WAF 保护）
  ├─ 307 → /WAF/VERIFY/CAPTCHA                滑动验证码页
  └─ 落地页 form(action=continue, nonce)
        └─ 303 → https://<panel>.skyvpn.bond  真正的 SSPANEL 面板
              ├─ POST /auth/login             返回 {ret, msg}
              └─ POST /user/checkin           返回 {ret, msg, traffic, trafficInfo}
```

关键点：

- 人机验证是 **GoEdge WAF 的滑动验证码**，不是 Cloudflare Turnstile。页面里有 `GOEDGE_WAF_CAPTCHA_ID` 隐藏字段和一个写死在 JS 里的 `nonce`，服务端只校验这两个值，**不做轨迹校验**。所以纯 requests 就能过，不需要 Playwright 或 Selenium。
- **面板域名是随机分配的**（形如 `q210ycweb.skyvpn.bond`、`subb3xq6b.skyvpn.bond`），唯一来源是入口站落地页的 continue 表单。
- **入口站 continue 有每 IP 配额**：实测连调 3 次，第 3 次起返回 429，之后会升级为空响应、再之后直接断连。GitHub Actions 的 runner IP 共享且常被限流，所以脚本**把发现过的面板域名缓存下来跨 run 复用**，只有缓存失效才回入口站。缓存用 `actions/cache` 持久化（`.panel-cache` 文件）。
- **面板站不需要入口站的 cookie**：`loginConfig.switch` 里 geetest / recaptcha 都是关的，`POST /auth/login` 直连可用。
- **面板站是另一个域名**，入口站的 `PHPSESSID` 不能带过去，脚本用两个独立 Session 按域名隔离。
- 面板可能开启两步验证，此时登录接口返回 `ret=2`，需要提供 `TWOFA_CODE`。
- 遇到 429 / 502 等临时错误会指数退避重试。

### 面板地址的解析顺序

按「稳 → 不稳」依次尝试，命中即停：

1. `PANEL_URL` 环境变量（可逗号分隔多个候选）
2. `.panel-cache` 里缓存的上次成功的域名
3. 入口站落地页跳转（消耗限流配额，最后手段）

### 本地运行

```bash
pip install requests
EMAIL=你的邮箱 PASSWORD=你的密码 python3 skyx-checkin.py
```

`PANEL_URL` 可省略（走缓存与入口站发现），`BASE_URL` 不填时默认 `https://www.skyxcloud.cc`。

### GitHub Actions

仓库里有 `.github/workflows/skyx-checkin.yml`，每天 UTC 22:00（北京时间 06:00）跑一次，也可手动触发。工作流用 `actions/cache/restore` + `actions/cache/save` 把面板域名跨 run 保存。

在 `Settings` → `Secrets` → `Actions` 添加：

| 参数 | 是否必须 | 内容 | 示例 |
| --- | --- | --- | --- |
| EMAIL | 是 | 注册机场所用邮箱 | a@example.com |
| PASSWORD | 是 | 注册机场所用密码 | password1 |
| PANEL_URL | 否 | 面板站地址，多个用逗号分隔 | https://xxx.skyvpn.bond |
| BASE_URL | 否 | 入口站地址 | https://www.skyxcloud.cc |
| TWOFA_CODE | 否 | 两步验证码（开了两步验证才需要） | 123456 |
| SCKEY | 否 | Server酱秘钥 | SCTxxxxxxxxxxxxxx |
| TGBOT | 否 | Telegram 推送 bot token | 5xxxxxxx:xxxxxxxxx |
| TGUSERID | 否 | Telegram 推送人 id | 8xxxxxxxxx |

> `PANEL_URL` 一般不用填——面板域名会变，填死了反而容易失效。交给脚本自己发现并缓存更稳。

### 测试与诊断

```bash
python3 test_skyx.py      # 本地 mock 服务器复刻整条链路，6 组断言，不碰真实站点
python3 probe429.py       # 诊断入口站限流：配额消耗规律与恢复窗口
```

`probe429.py` 会真的消耗入口站的限流配额，跑多了本机 IP 会被临时拉黑，别连续猛跑。

## 旧方案

- `main.py`：Xboard 类面板的 requests 版签到
- `DS-beta.py`：Playwright 版，处理 Cloudflare Turnstile 点击

这两个对 SKY VPN 都不适用了。

**使用 Github Actions 自动签到是违反了 Github Actions 条款的，如果仍需使用请 fork 后自行使用。**

做这个项目主要是看到最近冒出来许多机场注册就能白嫖，签到就送流量。然后又顺便拿这个项目学习一下 Github Actions。
请注意，本项目不讨论跟 fq 有关的话题，仅仅是技术讨论，不要询问白嫖 jc 等内容！！！

## 参考

1. https://github.com/zhjc1124/ssr_autocheckin 用了他的机场签到代码。
2. https://github.com/sirodeneko/genshin-sign 参考其 Actions 的 yml。

（两个仓库均无许可证，让我有点蒙）
最近仓库 https://github.com/sirodeneko/genshin-sign 增加了 GPL 许可证，在征求作者同意后，本仓库依旧保持使用 MIT 许可证进行开源，以下为与作者通信邮件

## 赞助我

[![](https://raw.githubusercontent.com/xiaocao666tzh/imghosting/main/img/%E4%B8%87%E8%83%BD%E6%94%B6%E6%AC%BE%E7%A0%81-%E8%8D%89%E3%81%AE%E5%8D%9A%E5%AE%A2.png)](https://raw.githubusercontent.com/xiaocao666tzh/imghosting/main/img/%E4%B8%87%E8%83%BD%E6%94%B6%E6%AC%BE%E7%A0%81-%E8%8D%89%E3%81%AE%E5%8D%9A%E5%AE%A2.png)
