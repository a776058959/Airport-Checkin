# SKY VPN 客户端分析报告

分析时间：2026-11-18（分两轮）
样本：`C:\Program Files\SKY VPN\`（已安装、已登录）
安装包：`D:\迅雷下载\SKY-VPN-Setup-0.6.5.exe`

---

## 最终结论

**客户端证书已经找到，但私钥拿不到，因此仍无法搬到 GitHub Actions。**

第二轮把证书本身挖出来了（`client-recon/certs/client.der`），但它是**只有公钥的证书**，私钥受 Windows CNG 保护、无法导出。实测证明服务端校验完整证书链，光有证书不够。

---

## 一、第二轮的主要突破

### 1. 解开了 session.dat（DPAPI）

```powershell
[ProtectedData]::Unprotect($bytes, $null, [DataProtectionScope]::CurrentUser)
```

得到明文（1126 字节 JSON），内容是 7 个 cookie：

| Cookie | 值 |
| --- | --- |
| `PHPSESSID` | r3h2pb0m3pmk04q7qiiba0hvdo |
| `email` | 776058959%40qq.com |
| `expire_in` | 1795545339 |
| `ip` | a7aebc6224021924c12cc0f5bc9f959e |
| `key` | 809c333c736b440fddb31251e737f517eeb7ff341df1d |
| `server_name_session` | af483a39578cbf53e6a3698e3fa52770 |
| `uid` | 4732 |

全部 domain 为 `client.skyvpn.bond`。**能从本机解出，但这是 DPAPI CurrentUser 加密，换机器/换用户就解不开。**

### 2. 从进程内存扫出了客户端证书

方法：`VirtualQueryEx` 遍历可读 `MEM_COMMIT` 区，`ReadProcessMemory` 分块读取，按 DER X.509 特征（`30 82 .. 30 82`）提取。

结果：439 个候选，去重 114 个，其中两个与 skyvpn 相关：

```
Subject : CN=SkyVPN Android Client, O=SkyVPN
Issuer  : CN=SkyVPN Client Auth CA, O=SkyVPN
Serial  : 0x7d32cd9b7b73e0a54f15e1f39ae1bfad
NotBefore: 2026-04-06 03:08:23 UTC
NotAfter : 2028-04-06 03:18:23 UTC
PubKey  : RSA
Thumb   : 72129AFE3CB8D4973CD75CB6F754B96D8C286B73

EKU     : clientAuth (1.3.6.1.5.5.7.3.2)
SAN     : client.skyvpn.bond, android-client@client.skyvpn.bond
KeyUsage: digitalSignature + keyEncipherment
```

已保存为 `client-recon/certs/client.der`。

**注意 Subject 是「Android Client」**——说明这是所有安卓/桌面用户**共用**的同一张证书。

### 3. 但私钥拿不到

| 检查项 | 结果 |
| --- | --- |
| 内存中该证书对象 `HasPrivateKey` | **False**（只有公钥） |
| 内存中的明文 PKCS#8 / PKCS#1 / EC 私钥 | 扫描 0 个 |
| `CurrentUser\My`、`LocalMachine\My` 等全部证书存储 | 无该 thumbprint 条目 |
| `%APPDATA%\Microsoft\Crypto\Keys`（CNG） | 有今日新建的密钥文件，但对应的是其他证书 |

私钥保存在 CNG（Key Storage Provider）里，由 OS 保护，程序只能"使用"不能"读出"。

### 4. 关键验证：服务端校验证书链

用抓到的证书 + 自签 RSA 私钥发起 mTLS：

```
TLS 握手：通过
服务端回应：TLSV1_ALERT_UNKNOWN_CA
```

**证明服务端做完整证书链校验**，必须有与这张证书配对的真私钥，伪造/替换都不行。

---

## 二、两套体系是分开的（重要）

| 域名 | 是否要 mTLS | 会话 |
| --- | --- | --- |
| `client.skyvpn.bond` | **要**（强制） | 客户端专用，cookie 带 `key`/`uid` |
| `q210ycweb.skyvpn.bond`（面板） | 不要 | 网页版，需账号密码登录 |

实测：把 `client.` 的 7 个 cookie 拿去 POST 面板域名 `/user/checkin`，返回 **302 → /auth/login**，说明两边会话不通用。

面板域名本身不要求证书（`/` 返回 200 完整 HTML，`/user/checkin` GET 返回 405 Method Not Allowed 说明端点存在），这就是现在网页脚本走的路。

---

## 三、为什么这条路走不通

用户设想：「证书部分用软件的、其余自己写」。技术前提是证书+私钥能脱离本机。现在的障碍是：

1. **私钥不可导出** —— CNG 保护，程序也只能调 API 用不能读
2. **service 进程有 DPAPI 相关的密钥操作** —— 即便找到私钥位置，也可能二次加密
3. **证书是所有用户共用的** —— 若真能导出并放进公开仓库的 Secrets，等于公开泄露

要继续只有一条路：**动态 hook**（Frida / Detours），在客户端建立 TLS 连接时截获 `ClientCertificateSelectionCallback` 的返回值，或 hook `SslStream` 拿到 `X509Certificate2` 对象后调用其签名操作做代理转发。这条路：

- 技术上可行（证书对象在进程内可用）
- 但**只能在 Windows 本机跑**，无法进 GitHub Actions
- 等于「本机常开一个转发服务」→ 与直接本机跑脚本没区别

---

## 四、现有方案对比

| 方案 | 能否在 Actions 跑 | 429 影响 | 维护成本 |
| --- | --- | --- | --- |
| **网页版（现状）** | 能 | 有，但三时段+保底已覆盖 | 低 |
| 客户端 API 直连 | **不能**（私钥不可导出） | 无 | — |
| 本机 hook 转发 | 不能（需 Windows 常开） | 无 | 高 |

**结论：维持现有网页方案。**

---

## 五、产出物

- `client-recon/REPORT.md` — 本文
- `client-recon/findings.json` — 结构化事实（已脱敏）
- `client-recon/certs/client.der` — 从内存提取的客户端证书（仅公钥，1315 字节）
