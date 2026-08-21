# tg115bot

高性能 Telegram → 115 网盘机器人：接收 TG 视频/文件，自动下载并上传到 115 网盘。

## 特性

- **MTProto 大文件下载**：多路并行分片（`stream_media` × N 路），绕过 Bot API 20MB 限制，支持 ≥2GB 视频
- **秒传优先**：服务端 SHA1 去重，命中即秒级落地，跳过整个上传阶段
- **OSS 直传**：秒传未命中时走阿里云 OSS 分片上传（V1 签名，含二次区间校验）
- **115 开放平台**：手写协议实现（零第三方 115 SDK 依赖），扫码授权，token 自动刷新
- **多账号轮转**：加权轮转 + 故障冷却，多账号分摊风控压力
- **频道监控**：订阅频道 + 关键词白/黑名单 + 目标目录规则，命中自动上传（图片/相册同样支持）
- **115 离线下载**：发磁力/ed2k/直链即自动离线（115 服务器下载，不占本地带宽），完成通知 + 失败自动重试
- **RSS 订阅**：订阅任意 RSS 源，新条目自动提取链接离线，关键词过滤 + 去重
- **电影订阅**：片名订阅追更，TMDB 匹配 + 资源发布自动离线（分辨率优先 + 中字加分）
- **频道回溯备份**：整频道历史媒体批量搬进 115，断点续传，队列背压防灌爆
- **文件管理**：`/ls` `/search` `/rm` `/mv` 在 TG 里遥控 115 网盘
- **分享链接转存**：发 115 分享链接（含访问码）自动转存
- **HTTP 直链中转**：`/dl` 本地中转下载后上传，与 115 离线互补
- **文件整理**：重命名模板（`{date}_{filename}` 等）+ 按扩展名归类子目录
- **Web 管理台**：实时进度、任务历史、账号状态、频道规则、日志查看
- **持久化**：SQLite 存任务历史与规则，重启不丢
- **安全与稳健**：凭据加密落盘、FloodWait 自动退避、磁盘水位保护、日志轮转

## 架构

```
TG 消息 → bot/handlers → core/queue → core/pipeline
                                      ├─ core/downloader   并行分片下载 + SHA1
                                      └─ core/uploader   → cloud115 秒传探测 → OSS 直传
```

## 快速开始

### 1. 部署代理（国内服务器）

TG 在国内服务器上无法直连，需先部署代理。项目自带一键部署脚本（mihomo/Clash 内核）：

```bash
sudo scripts/setup-mihomo.sh <订阅地址>     # 交互式输入亦可；支持本地 config.yaml 路径
```

装好后编辑 `config.yaml`（115/OSS 不走代理，国内直连更快）：

```yaml
telegram:
  proxy: "http://127.0.0.1:7890"    # mihomo 默认混合端口；socks5:// 同样支持
```

境外服务器跳过本节（`proxy` 留空）。

### 2. 安装依赖

需要 **Python ≥ 3.12**（Docker 镜像已内置）。TG 客户端使用 pyrofork（pyrogram 维护分支，API 一致）。

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> Debian/Ubuntu 系统 Python 直接 `pip install` 会被 PEP 668 拦截，请使用 venv 或 Docker。

### 3. 配置

```bash
cp config.yaml.example config.yaml
```

| 配置项 | 获取方式 |
|---|---|
| `telegram.api_id` / `api_hash` | https://my.telegram.org 申请 |
| `telegram.bot_token` | @BotFather 创建 bot 获取 |
| `telegram.user_session`（可选） | `python scripts/make_session.py` 生成，用于下载 >20MB 视频 |

### 4. 授权 115 账号

启动后向 bot 发送 `/auth`，会收到二维码，用 115 APP 扫码即完成授权；
也可在终端完成（会直接在终端渲染二维码）：

```bash
python scripts/check115.py --auth
```

token 自动保存到 `config/open_token_<账号名>.json`（可用 `TG115BOT_SECRET_KEY` 加密，见 `.env.example`）。

可选：跑一次上传链路自检（会在 115 的目标目录留 3 个测试文件，可删）：

```bash
python scripts/check115.py
```

### 5. 运行

```bash
python main.py
```

或 Docker 部署：

```bash
docker compose up -d --build
docker compose logs -f
```

## 使用

向 bot 发送视频/文件即可，文件会自动上传到 `upload.target_dir` 指定的 115 目录。

| 命令 | 说明 |
|---|---|
| `/start` `/help` | 帮助 |
| `/setdir <115路径>` | 设置目标目录（如 `/tg115bot/movies`） |
| `/auth` | 授权/检查 115 账号状态 |
| `/cancel` | 取消最近一个进行中的任务 |
| `/channels` | 查看频道监控规则 |
| `/addchannel <频道ID> <目标目录> [关键词...]` | 新增频道规则（关键词为白名单，留空=全部） |
| `/delchannel <规则ID>` | 删除频道规则 |
| `/offline <链接>` | 115 离线下载（磁力/ed2k/直链；**直接发链接也可**） |
| `/offlines` | 查看离线任务队列 |
| `/addrss <RSS地址> [目录] [关键词...]` | 订阅 RSS 自动离线（关键词=标题白名单，空=全部） |
| `/rsss` / `/delrss <ID>` | 查看 / 退订 RSS |
| `/sub <片名>` | 订阅电影，资源发布自动离线（需 nullbr API） |
| `/subs` / `/unsub <ID>` | 查看 / 取消电影订阅 |
| `/status` | 115 空间 / 离线配额 / 风控余量 / 账号 / 队列一览 |
| `/ls <路径>` | 列 115 目录 |
| `/search <关键词>` | 115 全盘搜索 |
| `/rm <路径>` | 删除（二次确认） |
| `/mv <源> <目的目录>` | 移动 |
| `/backup <频道ID或@名> [目录]` | 整频道历史备份（断点续传） |
| `/backups` / `/backupstop <ID>` | 备份进度 / 暂停 |
| `/dl <http直链>` | 本地中转下载后上传 |

### Web 管理台（可选）

`web.enable: true` 后，浏览器打开 `http://<host>:<port>/`（HTTP Basic 认证，凭据见 `config.web`）：

- 仪表盘 — 实时进度（3s 自动刷新）
- `/tasks` — 任务历史　`/accounts` — 账号状态　`/channels` — 频道规则在线增删　`/logs` — 日志

### 频道监控（可选）

1. 将 bot 加入目标频道；
2. `channel_monitor.enabled: true`；
3. 用 `/addchannel` 或 Web 台添加规则。频道 ID 可在 TG 内转发消息给 `@userinfobot` 获取。

### 离线下载

发送磁力 / ed2k / 种子及媒体直链给 bot 即自动离线下载（或 `/offline <链接>`），
资源在 **115 服务器**下载，不占本地带宽、不经代理。完成自动通知；失败自动重试 2 次。
`/offlines` 查看队列状态。

发 115 分享链接（`https://115.com/s/xxx?password=访问码`）自动转存到 `share.target_dir`
（需在 `config.yaml` 配置 `share.cookies`，浏览器登录后复制；该接口仅 Cookie 鉴权）。
`/dl <直链>` 走本地下载再上传（经 `telegram.proxy`），适合 115 离线不支持或慢的直链源。

### 频道回溯备份

```text
/backup -1001234567890 /tg115bot/archive
```

从最新消息向历史回溯，媒体全部入队上传（若该频道配了监控规则则按关键词过滤）。
**断点续传**：中断后重发命令自动从上次位置继续（kill -9 也不丢进度）；
每入队约 20 项发一次进度通知；`/backups` 查看进度，`/backupstop` 暂停。

### RSS 订阅（可选）

```text
/addrss https://example.com/feed.xml /tg115bot/pt 1080p HEVC
```

每 10 分钟检查一次全部订阅源；条目标题命中关键词（留空=全部）且含可下载链接
（magnet/ed2k/种子及媒体直链）时自动离线，重复条目自动去重。RSS 源经 `telegram.proxy` 抓取。

### 电影订阅（可选，需 nullbr API）

1. 到 https://nullbr.online/api 申请 API 授权；
2. `config.yaml` 填 `movie_sub.app_id` / `api_key`，重启；
3. `/sub 流浪地球` —— TMDB 匹配后入订阅表，每 4 小时检查资源，
   发布即自动离线（优先 ed2k，分辨率 2160p>1080p>720p，中字加分）。

## 配置项

完整说明见 `config.yaml.example` 内注释。常用：

| 键 | 默认 | 说明 |
|---|---|---|
| `telegram.proxy` | 空 | TG 代理（`socks5://` 或 `http://`）；115 不走代理 |
| `upload.target_dir` | `/tg115bot` | 115 默认目标目录 |
| `upload.workers` | 8 | TG 并行下载分片数 |
| `upload.oss_concurrency` | 8 | OSS 分片并发数 |
| `queue.concurrency` | 2 | 同时处理的任务数 |
| `accounts[].app_id` | 空 | 开放平台 AppID（空=内置公共 ID）；多账号配多条 + `weight` 权重 |
| `organize.rename_template` | `{filename}` | 重命名模板（`{date}/{name}/{ext}` 等） |
| `organize.classify_by_ext` | false | 按扩展名归入子目录 |
| `channel_monitor.enabled` | false | 频道监控开关 |
| `movie_sub.app_id/api_key` | 空 | nullbr API 授权（电影订阅，空=停用） |
| `movie_sub.target_dir` | `/tg115bot/movies` | 电影订阅保存目录 |
| `share.cookies` | 空 | 分享转存凭据（浏览器 Cookie；空=停用转存） |
| `web.enable` | false | Web 管理台开关 |
| `storage.min_free_gb` | 5 | 磁盘可用空间下限，低于则暂停接新任务 |

环境变量可覆盖任意配置：`TG115BOT__段__键=值`（双下划线分层，如 `TG115BOT__TELEGRAM__BOT_TOKEN`）。

## 项目结构

```
tg115bot/
├── main.py                # 入口
├── config.py              # 配置加载（yaml + 环境变量覆盖）
├── bot/                   # TG 客户端、命令处理、频道监控
├── core/                  # 下载/上传/离线/RSS/电影订阅/备份/直链/队列/整理
├── cloud115/              # 115 开放平台 + OSS 直传协议实现
├── persistence/           # SQLite 持久化
├── web/                   # Web 管理台（FastAPI + HTMX）
├── utils/                 # 限速/退避、凭据加密、日志
├── tests/                 # 单元测试（python tests/run_all.py）
└── scripts/
    ├── setup-mihomo.sh    # mihomo 代理一键部署
    ├── check115.py        # 115 授权与链路自检
    ├── make_session.py    # 生成 TG user session
    └── healthcheck.py     # 容器健康检查
```

## 注意事项

- 115 风控：内置请求最小间隔 + 抖动 + 指数退避，请勿把并发调得过高。
- token 过期自动刷新；彻底失效时 `/auth` 重新扫码即可。
- 大文件全程流式处理（预分配 + 分片 seek 写盘），不占额外内存。
- `downloads/` 为临时目录，上传成功后自动清理。

## 免责声明

仅供个人学习与合法的网盘文件管理使用。请遵守当地法律法规及 115 / Telegram 服务条款，使用风险自负。
