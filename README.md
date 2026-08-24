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
- **AI 助手模式（可选）**：配置任意 OpenAI 兼容模型（DeepSeek/Qwen/GPT…）后，直接用自然语言对话操作以上全部功能；AI 还能按需创建新的自定义工具（沙箱执行 + 确认启用）
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

### 一键初始化

```bash
git clone https://github.com/ivenlau/tg115bot.git && cd tg115bot
./scripts/init.sh
```

交互式完成 7 步（幂等，重跑安全，已就绪的自动跳过）：

| 步骤 | 内容 | 智能行为 |
|---|---|---|
| 1 | 系统依赖（Python ≥ 3.12） | 已装跳过，缺失自动 apt 安装 |
| 2 | 虚拟环境 + 依赖 | `.venv` + pip，装过自动跳过 |
| 3 | 代理（mihomo） | 能直连 TG 则跳过；已装直接采用；都不行引导输入订阅部署 |
| 4 | `config.yaml` | 交互收集 api_id/hash/bot_token、白名单（附获取指引） |
| 5 | 115 扫码授权 | 可当场扫或稍后在 TG 里 `/auth` |
| 6 | 可选功能 | AI 模式 / Web 台（回车跳过；Web 强提示改密码） |
| 7 | 完成提示 | 打印启动命令 |

初始化完成后：

```bash
./scripts/service.sh start
```

### 服务管理

```bash
./scripts/service.sh start      # 后台启动（nohup，SSH 断开不影响）
./scripts/service.sh stop       # 优雅停止（进行中上传会收尾，10s 后强杀）
./scripts/service.sh restart    # 重启（更新代码/改配置后用这个）
./scripts/service.sh status     # 状态：PID / 内存 / 运行时长
./scripts/service.sh log [N]    # 跟踪日志（默认尾部 50 行）
```

- PID 文件防重复启动（校验进程身份，防 PID 复用误杀）
- 双日志：`logs/stdout.log`（运行输出）+ `logs/tg115bot.log`（业务日志，10MB 轮转）

### 代理订阅更新

订阅过期/换机场时（mihomo 节点全挂、TG 失联）：

```bash
sudo scripts/setup-mihomo.sh <新订阅地址>    # 自动备份旧配置并实测连通性
./scripts/service.sh restart                 # bot 重连
```

### Docker 部署

```bash
# 1. 若需要代理（国内服务器访问 TG），先在宿主机部署 mihomo
sudo ./scripts/setup-mihomo.sh <订阅地址>

# 2. 配置环境变量
cp .env.example .env    # 填 telegram 段；代理填 http://host.docker.internal:7890

# 3. 启动
docker compose up -d --build

# 4. 查看日志
docker compose logs -f
```

### 手动安装

<details>
<summary>展开手动步骤</summary>

```bash
# 1. 依赖（Python >= 3.12；pyrofork 为 pyrogram 维护分支，API 一致）
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp config.yaml.example config.yaml    # 填 telegram 段与 proxy
# api_id/hash: https://my.telegram.org；bot_token: @BotFather
# 国内服务器 proxy: "http://127.0.0.1:7890"（mihomo 混合端口）

# 3. 115 授权（终端二维码扫码）
python scripts/check115.py --auth

# 4. 前台运行（调试用；长期运行建议 service.sh）
python main.py
```

可选：`python scripts/check115.py` 跑一次上传链路自检（会在 115 留 3 个测试文件，可删）。

</details>

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
| `/ai` `/aireset` `/aitools` | AI 模式开关 / 清空记忆 / 管理动态工具 |

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

### AI 助手模式（可选）

```yaml
ai:
  base_url: "https://api.deepseek.com/v1"   # 任意 OpenAI 兼容端点
  api_key: "sk-..."
  model: "deepseek-chat"
```

配置后普通文本消息即进入 AI 对话（命令与链接识别不受影响），例如：

- “帮我把流浪地球2存到网盘，要 4K” → 自动订阅追更
- “网盘还有多少空间？离线配额呢？” → 调 `full_status` 汇报
- “订阅这个 RSS，只要 1080p 以上” → `rss_add` + 关键词

AI 可调用全部内置工具（21 个）；需要新能力时它会写一个受限沙箱内的
Python 小工具，**经你在 TG 点确认后**才启用（`/aitools` 管理）。
会话记忆持久化，重启不丢；`/ai off` 临时停用。

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
| `ai.base_url/api_key/model` | 空 | AI 助手（OpenAI 兼容；空=停用） |
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
├── ai/                    # AI 助手：LLM 客户端/工具集/agent 循环/动态工具沙箱
├── cloud115/              # 115 开放平台 + OSS 直传协议实现
├── persistence/           # SQLite 持久化
├── web/                   # Web 管理台（FastAPI + HTMX）
├── utils/                 # 限速/退避、凭据加密、日志
├── tests/                 # 单元测试（python tests/run_all.py）
└── scripts/
    ├── init.sh            # 交互式初始化（依赖/代理/配置/授权 7 步）
    ├── service.sh         # 服务管理（start/stop/restart/status/log）
    ├── setup-mihomo.sh    # mihomo 代理部署/订阅更新
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
