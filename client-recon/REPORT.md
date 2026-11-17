# SKY VPN 客户端分析报告

分析时间：2026-11-18 凌晨
样本：`C:\Program Files\SKY VPN\`（已安装、已登录）
安装包：`D:\迅雷下载\SKY-VPN-Setup-0.6.5.exe`

---

## 一句话结论

**挖到了完整实现，但不能搬到 GitHub Actions 上跑。**

客户端签到链路已完整还原（`PanelApi.SignInAsync` → `ActionAsync` → `/user/checkin`），路径与网页版**完全一致**。但客户端走的是 **mTLS 双向认证**，服务器强制要求客户端证书，没有证书连 HTTP 请求都发不出去。证书不在安装包里，是运行时下载的，且会话 `session.dat` 用 DPAPI 加密（只能在你这台机器这个 Windows 账号下解）。

---

## 一、客户端技术栈

| 项 | 内容 |
| --- | --- |
| 运行时 | **.NET 10**（`NETCoreApp,Version=v10.0/win-x64`） |
| UI | Avalonia 11.3.23 + SkiaSharp |
| 核心程序集 | `SkyVpn.Core` 0.6.5、`SkyVpn.SetupPayload` 0.6.5（安装载荷，内含 mihomo） |
| 代理内核 | `core\mihomo.exe` 61 MB（Clash Meta） |
| 打包 | .NET single-file bundle，manifest 在文件末尾 |

### 解包方法（已验证可用）

manifest 在文件末尾，每条记录：

```
Offset(8) Size(8) CompressedSize(8) Type(1) PathLen(1) Path(PathLen)
```

压缩数据用 **raw deflate**（`zlib.decompressobj(-15)`）。已成功提取：

- `SkyVpn.Core.dll` — offset `0x2e68969`，size 3397632，csize 790994
- `SkyVpn.SetupPayload.dll` — offset `0x2f29b3b`，size 66071040，csize 23865080

---

## 二、完整 API 清单（`SkyVpn.Core.PanelApi`）

**Base URL：`https://client.skyvpn.bond`** → `163.52.242.61`（与网页面板同 IP）

签到链路：

```
SignInAsync      → POST /auth/login
BootstrapAsync   → 启动时拉账号信息
ActionAsync      → POST /user/checkin     ← 签到
ProfileAsync     → /profile
ClashProfileAsync→ 拉订阅
```

其余方法：`RecordsAsync`（流量记录）、`AnnouncementsAsync` / `NoticesAsync`（公告）、`SignOutAsync`、`ProductsAsync` / `OrdersAsync` / `BuyAsync` / `QuoteAsync` / `WalletAsync` / `CreateRechargeAsync` / `RechargeIsPaidAsync`（商店与充值）、`TicketsAsync` / `TicketAsync` / `CreateTicketAsync` / `ReplyTicketAsync`（工单）、`RegisterAsync` / `AuthConfigAsync` / `RequestPasswordResetAsync`、`UpdateEmailAsync` / `UpdateNicknameAsync` / `ResetInviteCodeAsync`、`ConversionPreviewAsync` / `DisableAutoRenewAsync` / `DeletePendingOrderAsync` / `InviteSummaryAsync` / `AccountDetailsAsync` / `TelegramBotAsync`。

**全部路径与网页版 SSPanel 一致**，没有任何新增的绕过能力。

---

## 三、决定性障碍：mTLS + 证书来源

`client.skyvpn.bond` 强制客户端证书。实测：

```
TLS1.3 无证书 -> 数据阶段 ssl3_read_bytes: certificate required
TLS1.2 无证书 -> ssl/tls alert bad certificate
```

证书信息：

- 引用路径 **`assets/certs/skyvpn-client.pfx.enc`**（`SkyVpn.Core.dll` 的 #Strings 堆里）
- **不在 SkyVPN.exe 里**（已全文搜索确认）
- **不在 SkyVpn.SetupPayload.dll 里**（已解压 66 MB 确认）
- 结论：**运行时从服务器下载**，且 `.enc` 说明还要解密
- 解密依赖 `System.Security.Cryptography.ProtectedData`（DPAPI）与 `X509CertificateLoader`

### 三层阻塞

| 层 | 问题 |
| --- | --- |
| 传输层 | 无客户端证书，连请求都发不出去 |
| 证书获取 | 要下载 + 解密 `.enc`，密钥在程序里，需继续逆向 |
| 会话 | `session.dat` 是 DPAPI 加密，**只能在本机本账号解密**，Actions 完全用不了 |

---

## 四、为什么客户端看起来更"健壮"

用户观察正确：客户端点签到从不报 429。

原因不是 API 更强，而是：

- 客户端用 **mTLS 认证官方客户端身份**，服务器把它当可信客户端，限流策略与入口站的匿名 continue 完全不同
- 网页版走 `www.skyxcloud.cc` 的**匿名** continue 接口，每 IP 有配额（实测约 40-50% 成功率）

**但客户端身份无法搬到 CI**：证书 + DPAPI 会话都是本机绑定的。把证书塞进 GitHub Secrets 也等于公开泄露所有用户共用的客户端凭据。

---

## 五、客户端为什么不显示已有签到状态

`settings.json` 的 `CheckedInOn` 是**客户端本地写的日期**（当前 `2026-11-18`），启动时**不向服务端查询签到状态**。

所以：在网页/工作流签到后，客户端仍显示"签到"按钮；只有点了客户端里的签到（`ActionAsync`）成功，才写本地日期并显示"已签到"。这与你描述的现象完全吻合。

---

## 六、投入产出评估

**要继续走客户端路线，必须做完：**

1. 找到 `assets/certs/skyvpn-client.pfx.enc` 的下载地址（需抓包或继续逆向 `PanelClient`）
2. 还原 `.enc` 的解密逻辑，拿到 pfx + 密码
3. 验证证书是否绑定 `DeviceUid`（若绑定，每台机器都要单独提取）
4. 解决 DPAPI 会话问题（Actions 上无解，除非 `/auth/login` 不需要 mTLS——实测整个域名都要）
5. 承担凭据公开风险

**而现有网页方案已经达标**：三时段概率抽取 + 保底 + 缓存，实测 run #3、#6、#7 全部成功，每天必成一次。429 只是让"某一次"失败，不影响"每天签上"。

---

## 七、产出物

- `client-recon/REPORT.md` — 本文
- `client-recon/findings.json` — 结构化事实（已脱敏）
- `_skyx/lib/SkyVpn.Core.dll` — 核心程序集 3.4 MB（临时目录）
- `_skyx/lib/SkyVpn.SetupPayload.dll` — 安装载荷 66 MB（临时目录）

---

## 八、追加验证（2026-11-18 02:xx，完整权限下）

用户提出「能不能直接用它的证书」后做了穷尽式排查，结论是**本机拿不到**：

| 位置 | 结果 |
| --- | --- |
| `C:\Program Files\SKY VPN\` 全目录 | 无 .pfx/.p12/.pem/.crt |
| `SkyVPN.exe`（73MB）全文 | 无 PKCS12 DER；`assets/certs/skyvpn-client.pfx.enc` 仅作为字符串出现 |
| `SkyVpn.Core.dll`（3.4MB，已从 bundle 解出） | PKCS12 DER 命中 **0**；证书名只在 #Strings 堆 |
| `SkyVpn.SetupPayload.dll`（66MB，已解压） | 无 pfx 字符串 |
| 进程内存（PID 16656 UI 进程，541MB） | 59 个 PKCS12，**全部**是 Microsoft 时间戳证书（exe 数字签名的证书链） |
| 进程内存（PID 972 service 进程，149MB） | 32 个 PKCS12，同样全是 Microsoft 时间戳 |
| 系统证书存储（CurrentUser\My / LocalMachine\My） | 无 skyvpn 相关 |
| 磁盘全盘（AppData / ProgramData） | 无 .pfx/.p12/.pem 命中 |

**结论：证书（`.pfx.enc`）是运行时从服务端下载的加密 blob，解密密钥在 .NET 10 程序内部。**

要拿到它必须：装 .NET 10 SDK → ilspycmd 反编译 `SkyVpn.Core.dll` → 定位 `PanelClient.DownloadAsync` 的下载地址 → 还原 `.enc` 解密逻辑（`ProtectedData` + 可能 AES）→ 得到 pfx 密码。

**这仍是本机可做的**，但需要 .NET 10 SDK（本机只有 .NET 8 运行时）。

### 关键教训（工具层）

- PowerShell 脚本**不能用中文**：本机 PowerShell 5.1 读 UTF-8 无 BOM 的 .ps1 会按 GBK 解码，中文变乱码导致字符串未闭合语法错误。所有 .ps1 必须纯英文。
- Windows PowerShell 5.1 基于 .NET Framework，**加载不了 .NET 10 程序集**（`0x80131515 Operation is not supported`），反射调用这条路需要 PowerShell 7+。
- 进程内存扫描用 `VirtualQueryEx` + `ReadProcessMemory` 遍历 `MEM_COMMIT` 且可读（0x02/0x04/0x08/0x20/0x40）区域有效，能抓到 91 个证书但对目标无用。
