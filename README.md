# LCC (Library Control CLI)

一个简单的命令行工具，对接 **北京航空航天大学（BUAA）图书馆** 的预约系统 `booking.lib.buaa.edu.cn`：查座、预约、签到/暂离/离馆、阅读灯亮度、番茄钟。

作者常用学院路一层西阅学空间（`area_id=8`），所以默认区域倾向它；沙河校区、其它阅览室等通过 `--area 名字或 id` 指定（见下文「列出区域」）。

## 免责声明

- 请确保你的使用符合学校/图书馆的服务条款与相关规定。
- `token` / `cookie` 属于敏感凭证，请勿泄露；本项目已在 `.gitignore` 里忽略 `.lcc.json` 和 `.env`。

## 安装

推荐用 [pipx](https://pipx.pypa.io/) 一键安装（需要 Python 3.9+，以及系统有 `openssl`——macOS/Linux 默认都有）：

```bash
# 没有 pipx 的话先装一次：
#   macOS:  brew install pipx && pipx ensurepath
#   Linux:  python3 -m pip install --user pipx && python3 -m pipx ensurepath

pipx install git+https://github.com/KawaroX/lcc.git
```

装完后终端里直接用 `lcc`：

```bash
lcc --help
```

升级 / 卸载：

```bash
pipx upgrade lcc
pipx uninstall lcc
```

### 开发模式

```bash
python3 lcc.py --help
# 或：
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

## 登录

最省事的方式：在当前目录放一个 `.env`（参考 `.env.example`），写入：

```
LCC_USERNAME=你的学号
LCC_PASSWORD=你的SSO密码
```

然后：

```bash
lcc login
```

成功后会把 token / cookie 写到 `.lcc.json`。后续命令在 token 过期时会自动刷新。

如果不想把密码放文件里，运行 `lcc login --username 你的学号`，会交互式提示密码。

## 常用命令

```bash
lcc me                         # 当前座位 / 亮度 / 时段摘要
lcc me --raw                   # 原始 subscribe 响应（调试用）

lcc book                       # 列出默认区域的空闲座位，等你输入座位号
lcc book 131                   # 直接预约 no=131 的座位
lcc book --id 276              # 按座位 id 预约
lcc book --area 三层西 131      # 指定区域

lcc signin                     # 签到（到馆）
lcc leave                      # 暂离
lcc checkout                   # 离馆

lcc seats                      # 默认区域的空闲座位
lcc seats --area 一层西 --all  # 某区域所有状态（含已占用）

lcc areas                      # 所有校区/楼层/区域（树形）
lcc areas --flat               # 扁平：id  完整路径  free/total
lcc areas --refresh            # 跳过 24h 缓存重新拉取

lcc light 30                   # 亮度 30
lcc light on                   # 等价于 light 20
lcc light off                  # 等价于 light 0

lcc pomo                       # 25m，结束时闪灯 20↔40 两次
lcc pomo 45                    # 45m
lcc pomo 1h                    # 1 小时（只支持 m / h 后缀）
lcc pomo 25 15 60              # 25m，闪 15↔60
lcc pomo 25 --flash 15:60      # 同上，flag 写法

lcc config --default-area 一层西   # 设置默认区域
```

## 按名字指定区域

所有接受 `--area` 的命令，以及 `book` 的交互列表，都既接受 id 也接受名字：

- 纯数字当 id 用（不走网络）
- 非数字在区域树里做大小写不敏感的子串匹配（先精确名字，再 `nameMerge` 包含）
- 唯一命中 → 用它；多命中 → 列出候选让你缩小范围；零命中 → 报错

例子：

```bash
lcc seats --area 一层西        # → 学院路一层西阅学空间 (id=8)
lcc seats --area 102阅学       # → 沙河一楼 102 阅学空间 (id=63)
lcc book --area 六层西         # → 学院路六层西中文借阅室 (id=29)
```

模糊词可能匹配多个（例如 `三楼`），此时 CLI 会列出候选让你缩小范围。

## 环境变量

- `LCC_USERNAME` / `LCC_PASSWORD` — `lcc login` 使用的 SSO 凭证
- `LCC_TOKEN` / `LCC_COOKIE` — 覆盖 `.lcc.json` 里的 token/cookie
- `LCC_DEFAULT_AREA_ID` — 默认区域（也可用 `lcc config --default-area` 写入 `.lcc.json`）
- `LCC_PROXY=1` — 走系统代理（默认不走，因为通常在校园网内）
- `LCC_INSECURE=1` — 跳过 HTTPS 证书校验（不推荐）

全局 flag（可放在任何位置）：

```bash
lcc --proxy seats              # 这次走系统代理
lcc --insecure login           # 这次跳过证书校验
```

## 区域编号参考

权威来源是实时接口 —— `lcc areas` 或 `lcc areas --flat`。下面是一个快速速查（仅用于了解结构；数量可能变动）：

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

## SSL 证书校验失败

如果遇到 `CERTIFICATE_VERIFY_FAILED`，通常是本机 Python 没有正确安装/找到系统根证书：

- macOS 用 python.org 安装包时，运行一次 `Install Certificates.command`；
- 或在 Python 环境里 `pip install --upgrade certifi`。

临时绕过（不推荐）：

```bash
lcc --insecure login
```

## 安全提示

- `.lcc.json` 和 `.env` 里是你的 token/cookie/账号密码，不要提交到 Git。
- Token 是 JWT，包含学号、姓名等个人信息；贴抓包/日志时请脱敏。
- 怀疑泄露了 token，重新 `lcc login` 会让旧 token 作废；必要时修改 SSO 密码。
