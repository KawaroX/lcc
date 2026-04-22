# bhlib

命令行工具，对接 **北京航空航天大学（BUAA）图书馆** 的预约系统 `booking.lib.buaa.edu.cn`：查座、预约、签到/暂离/离馆、阅读灯亮度、番茄钟。

## 免责声明

- 请确保你的使用符合学校/图书馆的服务条款与相关规定。
- `token` / `cookie` / 账号密码存在用户配置目录下的 `bhlib/config.json`（会尝试设为权限 0600），请勿泄露。

## 安装

需要 Python 3.9+。

```bash
pipx install git+https://github.com/KawaroX/bhlib.git
```

（没装 pipx 的话先 `brew install pipx && pipx ensurepath` / Linux 用 `python3 -m pip install --user pipx` / Windows 用 `python -m pip install --user pipx && python -m pipx ensurepath`。）

> Windows 备注：
>
> - 配置文件默认在 `%APPDATA%\\bhlib\\config.json`（例如 `C:\\Users\\<用户名>\\AppData\\Roaming\\bhlib\\config.json`）。
> - 会尝试把配置文件权限设为 `0600`（`chmod`），但 Windows 上可能不生效；不影响运行，只是权限语义不同。
> - `seats` 的平面图输出使用 ANSI 颜色；如果显示异常可用 `bhlib seats --list` 或 `bhlib config --seat-format list`。

升级 / 卸载：

```bash
pipx upgrade bhlib
pipx uninstall bhlib
```

## 登录（一次即可）

```bash
bhlib login
# 学号: ********
# 密码: ********
# OK: 登录成功，配置已写入 <config path>
```

之后**从任何目录**跑 `bhlib` 都能用。token 到期时会用保存的账号密码自动续约，不需要任何 `.env` 或环境变量。

## 常用命令

```bash
bhlib me                         # 当前座位 / 亮度 / 时段摘要
bhlib me --raw                   # 原始 subscribe 响应（调试用）

bhlib book                       # 列出默认区域的空闲座位，等你输入座位号
bhlib book 131                   # 直接预约 no=131 的座位
bhlib book --id 276              # 按座位 id 预约
bhlib book --area 三层西 131      # 指定区域
bhlib book --all                 # 列出时展示全部座位（含已占用/已预约）

bhlib signin                     # 签到（到馆）
bhlib leave                      # 暂离
bhlib checkout                   # 离馆

bhlib seats                      # 默认区域的座位状态（默认输出终端平面图）
bhlib seats --all                # 显式要求显示全部（与默认行为相同）
bhlib seats --list               # 以列表形式输出（仅空闲座位）
bhlib seats --all --list         # 列表形式输出全部座位
bhlib seats --area 一层西        # 指定区域
bhlib seats --image              # 生成 PNG 平面图（保存到系统临时目录）
bhlib seats --image --image-path ./seat.png   # 指定图片保存路径
```

> **平面图自动 `--all` 说明**：`seats` 默认以**平面图（`--map`）**展示。为了让你一眼看到整块区域的使用情况，只要最终输出是平面图（无论是默认、显式 `--map`，还是配置默认格式为 `map`），系统都会**自动显示全部座位状态**，无需再手动加 `--all**。如果你只想看空闲座位，请使用 `--list`。

![seats 平面图示例](assets/seats-map-demo.png)

```bash
bhlib areas                      # 所有校区/楼层/区域（树形）
bhlib areas --flat               # 扁平：id  完整路径  free/total
bhlib areas --refresh            # 跳过 24h 缓存重新拉取

bhlib light 30                   # 亮度 30
bhlib light on                   # 等价于 light 20
bhlib light off                  # 等价于 light 0

bhlib pomo frontend              # 前台运行 25m，结束时闪灯 20↔40 两次
bhlib pomo frontend 45           # 前台 45m
bhlib pomo frontend 1h           # 前台 1 小时（只支持 m / h 后缀）
bhlib pomo frontend 25 15 60     # 25m，闪 15↔60
bhlib pomo frontend 25 --flash 15:60   # 同上，flag 写法

bhlib pomo start                 # 后台启动番茄钟守护进程（默认 25m）
bhlib pomo start 45              # 后台 45m
bhlib pomo status                # 查看后台番茄钟状态
bhlib pomo stop                  # 停止后台番茄钟
bhlib pomo flash                 # 立即闪烁灯光（不启动计时器）

bhlib config --default-area 一层西   # 设置默认区域
bhlib config --seat-format list      # 设置 seats 默认输出为列表（默认是 map 平面图）
```

## 输出格式

`signin`、`leave`、`checkout`、`light` 以及 `book` 等操作成功后，终端会直接显示友好的文本提示，例如：

```
✅ 签到成功
✅ 临时离开操作成功，请在 2026-04-22 14:08 前返回
✅ 成功
```

如果接口返回错误（`code != 0`），则会显示：

```
❌ [10001] token 已过期
```

需要查看原始 JSON 时，可在 `me` 等支持 `--raw` 的命令中使用该参数。

## 按名字指定区域

所有接受 `--area` 的命令，以及 `book` 的交互列表，都既接受 id 也接受名字：

- 纯数字当 id 用（不走网络）
- 非数字在区域树里做大小写不敏感的子串匹配（先精确名字，再 `nameMerge` 包含）
- 唯一命中 → 用它；多命中 → 列出候选让你缩小范围；零命中 → 报错

例子：

```bash
bhlib seats --area 一层西        # → 学院路一层西阅学空间 (id=8)
bhlib seats --area 102阅学       # → 沙河一楼 102 阅学空间 (id=63)
bhlib book --area 六层西         # → 学院路六层西中文借阅室 (id=29)
```

## 区域编号参考

权威来源是实时接口 —— `bhlib areas` 或 `bhlib areas --flat`。下面是一个快速速查（仅用于了解结构；数量可能变动）：

**校区（premise）**

| id | 名称 |
|---:|---|
| 9 | 学院路校区图书馆 |
| 55 | 沙河校区图书馆 |
| 2 | 沙河校区特色阅览室 |

**学院路常用区域**

| id | 区域 |
|---:|---|
| 8 | 一楼/一层西阅学空间 |
| 16 | 一楼/一层东报刊阅览室 |
| 18 | 二楼/二层东中文借阅室 |
| 19 | 二楼/二层西知行书斋 |
| 20/21/22 | 三楼/三层南 东·中·西 |
| 23/24/25 | 四楼 东·中·西 |
| 27 | 五楼/五层西新书借阅室 |
| 28/29 | 六楼 东·西 |

**沙河常用区域**

| id | 区域 |
|---:|---|
| 63/117/64 | 一楼/102、103、104 阅学空间 |
| 65/68/67 | 二楼/201南、二层中央、二层西 |
| 69/71/72/73 | 三楼/301南、314北、三层西、三层中央 |
| 82/83 | 六楼/601南、613北 |

## 可选：环境变量覆盖

正常使用不需要。给 CI / 脚本 / 临时切账号用：

- `BHLIB_USERNAME` / `BHLIB_PASSWORD` — 覆盖 config 里存的凭证
- `BHLIB_TOKEN` / `BHLIB_COOKIE` — 覆盖 config 里的 token / cookie
- `BHLIB_DEFAULT_AREA_ID` — 覆盖默认区域
- `BHLIB_PROXY=1` — 走系统代理（默认不走，因为通常在校园网内）
- `BHLIB_INSECURE=1` — 跳过 HTTPS 证书校验（不推荐）

全局 flag（可放在任何位置）：

```bash
bhlib --proxy seats              # 这次走系统代理
bhlib --insecure login           # 这次跳过证书校验
```

## 安全提示

- 配置文件 `bhlib/config.json` 里是明文的账号密码 + token（具体路径取决于系统）。
- Token 是 JWT，包含学号、姓名等个人信息；贴抓包/日志时请脱敏。
- 怀疑泄露了 token，重新 `bhlib login` 会让旧 token 作废；必要时修改 SSO 密码。

## 证书说明

`booking.lib.buaa.edu.cn` 的 HTTPS 证书链**只发送叶子证书**，不带 GlobalSign 中间 CA。浏览器和 macOS Keychain 会自动 AIA fetch，但 Python `urllib` 不会。本项目把 `GlobalSign GCC R3 DV TLS CA 2020` 中间证书打包进 `src/bhlib/certs/booking_ca.pem`，并加到 SSLContext 里，所以装完即用，不需要额外 `pipx inject certifi` 或 `Install Certificates.command`。
