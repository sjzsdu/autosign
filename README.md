# autosign — Trae CN / Qoder CN / CodeBuddy / 豆包 每日签到

幂等、可重复执行的每日签到脚本，把每天能领的 credits / 额度收敛成一条命令：
Trae、Qoder、CodeBuddy 直接读本机桌面端登录态、走官方接口；豆包的写接口强制
浏览器现场生成 `a_bogus` 签名，所以用 Playwright 驱动本机 Chrome 无头完成
（只需扫码登录一次）。领取前都先查状态，今天领过就直接返回，不会重复领。

## 目录结构

```
autosign/
├── run.sh                  # 入口：签到 / 豆包登录 / 装 cron / 看日志
├── scripts/
│   ├── trae_signin.py      # Trae CN（Python 标准库 + openssl）
│   ├── qoder_signin.py     # Qoder CN（Python 标准库 + openssl + libsecret）
│   ├── codebuddy_signin.py # CodeBuddy（Python 标准库，读插件登录态）
│   └── doubao_signin.py    # 豆包（Playwright + 本机 Chrome）
├── .venv/                  # 可选：豆包用的 Playwright，不入库
├── logs/
│   ├── autosign.log        # 结构化日志（每行 [时间] [级别] 内容）
│   └── cron.out            # 定时任务的原始输出
└── README.md
```

## 快速开始

```bash
./run.sh                # 签到（幂等，重复跑无副作用）
./run.sh status         # 只查状态，不领取
./run.sh doubao-login   # 豆包首次扫码登录（有窗口，只需一次）
./run.sh install-cron   # 安装定时任务：每天 09:07 / 15:07 / 21:07 各跑一次
./run.sh uninstall-cron # 卸载定时任务
./run.sh log 50         # 看最近 50 行日志
./run.sh help
```

退出码：`0` 正常（含 `CLAIMED` / `ALREADY` / `INACTIVE` / `PENDING`），
`1` 硬失败（网络、接口、风控），`2` 凭据/用法错误。

依赖：`python3` + `openssl`，Ubuntu 默认自带，不需要 Node.js。Qoder 额外依赖
系统的 libsecret/GNOME Keyring（桌面版 Ubuntu 默认自带）。豆包额外需要 Playwright
和本机 Chrome/Chromium：

```bash
python3 -m venv .venv && .venv/bin/pip install playwright   # 或 uv pip install
./run.sh doubao-login                                      # 扫码一次
```

`run.sh` 检测到项目内 `.venv/bin/python3` 会优先使用；没装 Playwright 或没登录时
只跳过豆包，不影响其余签到和 cron。

## 输出契约

每个签到脚本输出一行 JSON，人话在 `report` 字段：

```json
{"result": "CLAIMED", "report": "成功领取 200 积分（当前积分 300）", "credits": 300, "diag": {...}}
```

| result | 含义 | 是否失败 |
|--------|------|---------|
| `CLAIMED` | 本次领取成功 | 否 |
| `ALREADY` | 今天已领过（接口幂等） | 含 |
| `INACTIVE` | 签到活动未开启 | 否 |
| `PENDING` | 今日可领但未领取（`status` 模式） | 否 |
| `NO_SESSION` | 登录态过期，需重新登录对应客户端 | 是 |
| `NO_AUTH` | 本机没找到凭据（Qoder 还包括客户端未运行；豆包还包括未扫码登录 / 未装 Playwright） | 是 |
| `NETWORK` / `ERROR` | 网口或服务端异常 | 是 |

`OK` 只由 `doubao_signin.py login` 使用，表示扫码登录成功（不参与每日签到结果判定）。

`diag` 字段是排查用的现场快照：Trae 有 `app_version` / `package_type` / `req_source` /
`os_name` / `device_model` / `device_id_set`，Qoder 有 `auth_source` / `campaigns` /
`claimable` / `claimed` / `credits_source` / `expires`，CodeBuddy 有 `auth_file` /
`domain` / `uid_set` / `token_expires_at` / `streak_days` / `theme`，豆包有
`app_version` / `ua_samantha` / `state_file` / `state_exists` / `url` / `headless`。
任何情况下脚本都不会打印 token / Cookie。

## 曾经的坑（已修复）

原 `run.sh` 是一整份被注释掉的 Node.js 方案，且依赖一个不存在的 `cloud-signin-kit`
项目。真正跑通需要解决的是**请求头与客户端 1:1 对齐**，否则服务端风控一律返回
`9074 当前参与用户太多`：

| 请求头 | 旧值（错误） | 正确值（= 真实客户端） |
|--------|------------|----------------------|
| `x-device-id` | `telemetry.devDeviceId`（UUID） | `iCubeAuthInfo://icube-dc:<数字ID>`，即 `guaranteedDeviceId` |
| `x-app-version` | 写死 `3.4.3` | `product.json` 的 `appVersion`（当前 `3.4.4`） |
| `x-device-type` | 写死 `macOS` | `os_name` → Linux 上是 `linux` |
| `x-os-version` | 内核版本号 | `/etc/os-release` 的 `PRETTY_NAME`（如 `Ubuntu 24.04.5 LTS`） |
| `x-device-brand` | `platform.machine()` | `/sys/class/dmi/id/product_name` |
| `req_source` | 写死 `2` | `packageType in (SOLO_*) ? 2 : 1`，`TRAE_CN` → `1` |

路径探测也补全了 Linux（`~/.config/Trae CN/User/globalStorage/storage.json`、
`/usr/share/trae-cn/resources/app/product.json`）与 Windows。

排查方法：客户端自己的日志会记录它调了哪些接口，可与脚本对比——

```bash
grep -h 'checkin_credits' ~/.config/Trae\ CN/logs/*/main.log
```

## Qoder CN

Qoder 的签到入口是**活动（campaign）**，不是通知已读。脚本直调：

| 用途 | 请求 |
|------|------|
| 查活动 | `GET https://openapi.qoder.com.cn/sash/api/v1/me/campaigns` |
| 领取 | `POST .../sash/api/v1/me/campaigns/<campaignId>/claim` |
| 余额 | `GET .../sash/api/v2/me/usage` |

请求头必须包含：

- `Authorization: Bearer <token>`
- `X-Request-ID: <uuid4>`
- **`Cosy-ClientType: 10`** —— 缺这个头时服务端返回 `200 + campaigns: []`，
  表现为“今日暂无可领取的签到活动”，即使客户端里明明可以领
- `Cosy-Version: 0.5.2`（可选，带上看齐客户端）

`campaigns[]` 里 `actionType=CLAIM_BENEFIT` 且 `claimStatus=CLAIMABLE` 的活动才是
可领签到；`claimStatus=CLAIMED` → `ALREADY`；没有 `CLAIM_BENEFIT` 活动 →
`INACTIVE`。`hasClaimableCampaign` 会被“首次订阅福利”等 `VIEW_DETAILS` 推广置为
true，**不能单独作为签到判断依据**。

注意：`GET /sash/api/v1/me/notifications` 返回的是推广/通知，`POST .../read` 只是
把通知标已读，**不会发放 credits**；真正发 credits 的是
`POST /sash/api/v1/me/campaigns/<campaignId>/claim`。

单独调试：`python3 scripts/qoder_signin.py status|auto|claim`。
脚本只打印一行 JSON，绝不输出 token / refreshToken。

### 凭据来源（Linux 实测，与 HarmonyOS 分支不同）

`~/.config/com.qodercn.app.stable/auth.v1.dat` 实际是 Chromium/Electron
safeStorage（OSCrypt）的 `v11` 封装，不是 HarmonyOS 的 `QASSET1\0` + AES-256-GCM：

- 文件 = `"v11"` + AES-128-CBC 密文（PKCS#7），IV 固定 16 个空格；
- 密钥 = `PBKDF2-HMAC-SHA1(password, "saltysalt", 1, 16)`；
- `password` 是 GNOME Keyring 里的 “Chromium Safe Storage” 条目
  （schema `chrome_libsecret_os_crypt_password_v2`，attribute
  `application=Qoder CN App`），由 libsecret 管理；
- 脚本用 `ctypes` 调 libsecret（只依赖标准库），失败时回退 PyGObject /
  `secret-tool`，再回退 Electron 无钥匙串时的 `peanuts` 固定口令；
- 解密用 `openssl enc -d -aes-128-cbc`，不引入 `cryptography` 依赖。

因此 Linux 端不需要 ArkTS 的 `QoderCredentialStorage.GetWrappedKeyV1`，也不需要
`--remote-debugging-port` 的 CDP 路径。HarmonyOS 分支的 `QASSET1\0` 信封脚本会明确
报 `NO_AUTH`，不会误读。

### 与 Trae 的差异

| 项目 | Trae CN | Qoder CN |
|------|---------|----------|
| 鉴权头 | `Cloud-IDE-JWT <token>` | `Bearer <token>` + `Cosy-ClientType: 10` |
| 领取语义 | 独立 `checkin_credits/claim` 接口 | `campaigns/<id>/claim` |
| 运行前提 | 读取本地 storage.json，客户端不必运行 | 按验收约定要求客户端运行；未运行 → `NO_AUTH` |
| 状态判定 | 接口直接返回 `checked_in` | `campaigns[].claimStatus`（只认 `CLAIM_BENEFIT`） |
| credits | 签到接口返回 | `/sash/api/v2/me/usage` 的 `remaining`（套餐 + 加油包） |
| 登录态 | 约 7 天过期，客户端续期 | `expiresAt` 由客户端续期；过期报 `NO_SESSION` |

签到活动有明确的 `claimStatus`，脚本直接以它为准；成功后把日期、金额和
`campaignKey` 写入 `~/.cache/autosign/qoder.json` 作为同一台机器的兜底。活动
每日 10:00（UTC+8）刷新，已领过的当天再次执行会返回 `ALREADY`，不会重复领取。

### 失败时的处置

| result | 处置 |
|--------|------|
| `NO_AUTH`（未运行） | 打开 Qoder CN 客户端并登录。cron 条目默认已带 `QODER_ALLOW_OFFLINE=1`，客户端没开也会用有效 token 签到；手动执行时可显式设置该变量 |
| `NO_AUTH`（钥匙串） | 解锁桌面会话 / 登录 GNOME Keyring；确认 `secret-tool lookup application "Qoder CN App"` 能取到值；否则重新登录 Qoder |
| `NO_SESSION` | token 过期或被吊销，打开客户端重新登录 |
| `INACTIVE` | 当前没有 `CLAIM_BENEFIT` 活动；打开客户端活动页确认是否已结束 |
| `PENDING` | 活动可领但 `status` 模式不领取；运行 `./run.sh` 或 `claim` |
| `NETWORK` / `ERROR` | 检查网络/代理后重试；`ERROR` 时打开客户端活动页确认。若一直 `INACTIVE`，先检查 `Cosy-ClientType: 10` 是否被代理/网关改写 |

`run.sh` 对 Qoder 的 `NO_AUTH` / `NO_SESSION` 只告警并跳过，不会让 Trae 签到或
整轮 cron 失败；其他结果按原契约计入失败。

调试开关（一般不需要）：
`QODER_AUTH_FILE`、`QODER_KEYRING_APP`、`QODER_API_BASE`、`QODER_TIMEOUT`、
`QODER_CLIENT_TYPE`（默认 `10`）、`QODER_STATE_FILE`、`QODER_ALLOW_OFFLINE`、
`QODER_PROCESS_MATCH`。

## CodeBuddy

CodeBuddy 的 IDE 插件登录后把明文登录态写在
`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`
（Linux `~/.config/...`，Windows `%APPDATA%\...`），脚本直接读取：

| 用途 | 请求 |
|------|------|
| 查状态 | `POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status` |
| 领取 | `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |

请求头：`Authorization: Bearer <accessToken>`、`X-User-Id: <uid>`、
`X-Domain: www.codebuddy.cn`，请求体 `{}`。状态接口一次返回活动名、连续天数
`streak_days`、今日额度 `daily_credit` 和总余额 `total_credits`，领取成功返回
`data.credit`。

两个常见坑：`Authorization` 必须带 `Bearer ` 前缀（否则 401）；路径必须带
`/v2/` 且用 `www.codebuddy.cn`（旧教程里的 `copilot.tencent.com/billing/...`
会 404）。新版 WorkBuddy 桌面端把 token 改成 `$wbEncrypted` 信封加密，脚本不读它，
请用 CodeBuddy IDE 的明文登录态；token 约 28 天过期，打开一次 IDE / 插件即可续期。

## 豆包

豆包的「天天领额度」写接口（签到、领奖）强制 `a_bogus` 动态签名，签名由网页 JS
在真实浏览器里用参数 + 时间戳 + 指纹现场算出，只带 Cookie 裸调会被拒绝。所以脚本
用 Playwright 开一个真实 Chromium：页面自己签名发请求，脚本只负责点「立即签到」。

```bash
./run.sh doubao-login   # 一次性：弹出浏览器 → 点登录 → 手机扫码
./run.sh                # 之后无头自动签到
```

- 登录页用普通 Chrome UA（`SamanthaDoubao` UA 下不显示登录入口）；登录态（Cookie
  + localStorage）存到 `~/.cache/autosign/doubao.state.json`（0600）。
- 签到页必须用 `SamanthaDoubao/<客户端版本>` UA，否则提示「该活动仅支持在豆包电脑版
  参与」。版本号自动读 `/Applications/Doubao.app` 的 `Info.plist`。
- 判定顺序：卡片 `[data-reward-code="daily_signin"]` 显示「立即签到」才点击；显示
  「已签到」直接返回 `ALREADY`；领取后重新读卡片复核，避免误报。
- 登录态失效会跳回登录页，脚本识别后返回 `NO_SESSION` 并提示重新 `doubao-login`；
  `run.sh` 对豆包的 `NO_AUTH` / `NO_SESSION` 只告警并跳过，不影响其他签到。

调试开关：`DOUBAO_HEADED=1`（看得见窗口）、`DOUBAO_TIMEOUT`、`DOUBAO_LOGIN_TIMEOUT`、
`DOUBAO_STATE_FILE`、`DOUBAO_CHECKIN_URL`、`DOUBAO_APP_VERSION`、
`DOUBAO_BROWSER_CHANNEL`（默认自动找 Chrome/Edge）。失败时会截图到
`~/.cache/autosign/doubao-error.png`；扫码登录时设 `DOUBAO_DEBUG_SHOT=/tmp/login.png`
可把登录页截下来排查。

## 注意事项

- **Trae 登录态约 7 天过期**（`auth.expiredAt`），过期后脚本报 `NO_SESSION`。
  打开一次 Trae 桌面端即可自动续期，无需改脚本。
- **CodeBuddy token 约 28 天过期**；过期报 `NO_SESSION`，打开一次 IDE / 插件续期。
- **豆包登录态由扫码会话维持**；失效后重跑 `./run.sh doubao-login` 即可。
  豆包签到依赖本机 Chrome 与 Playwright，只影响豆包这一项。
- **Qoder 登录态由客户端续期**；手动执行时客户端未运行会按验收约定报 `NO_AUTH`
  并跳过，需要离线签到可显式设置 `QODER_ALLOW_OFFLINE=1`。
- **cron 条目默认带 `QODER_ALLOW_OFFLINE=1`**，不依赖 Qoder 客户端常开；修改后
  需要重新执行 `./run.sh install-cron` 才会更新 crontab。
- 定时任务用 `flock -n` 加锁，上一次没跑完会直接跳过，不会并发叠加。
- 每天 3 个时间点全部是幂等请求：没领到会自动重试，领到了就打印 `ALREADY` 直接返回。
- Trae 服务端偶发 `9074` 是容量护栏，脚本会按 `0/5/20/45s` 退避重试 4 次。

## 致谢

签到接口协议与 `iCubeAuthInfo` 的 AES-128-CBC 解密布局参考了
[agent-auto-signin](../agent-auto-signin)（MIT）中针对 macOS 的实现，本仓库在其基础上
做了 Linux/Windows 适配与上述风控参数修正。豆包的浏览器签到流程参考了社区里
Playwright 方案的页面判定（`SamanthaDoubao` UA、签到卡片与登录引导），本仓库做了
登录态持久化与统一的 JSON 输出适配。

## License

MIT
