# tg115bot

高性能 Telegram → 115 网盘机器人：把 TG 视频/文件临时下载到本地，再上传到 115 网盘。

## 特性

- **MTProto 下载**（Pyrogram v2 + TgCrypto），绕过 Bot API 20MB 限制，可下大视频（≥2GB）
- **多路并行分片下载**（`stream_media(offset,limit)` × N 路），打满下行带宽
- **秒传优先**：115 服务端 SHA1 去重，命中即跳过整个上传阶段（秒级落地）
- **OSS 分片并发上传**（秒传未命中时），流式、内存态断点重试
- **边下边算 SHA1**：单 worker 时下载内联哈希，零额外读盘；多 worker 时下完一次顺序读算
- **115 开放平台**：手写协议（零 p115 SDK 依赖），/auth 扫码授权（PKCE），token 自动刷新
- **多账号轮转**：加权轮转 + 故障冷却，多账号分摊风控
- **频道监控**：订阅频道 + 关键词白/黑名单 + 目标目录规则，命中自动上传
- **重命名/归类**：模板（`{date}_{filename}` 等）+ 按扩展名归类子目录
- **Web 管理台**：FastAPI + HTMX，实时进度 / 任务历史 / 账号 / 频道规则 / 日志
- **持久化**：SQLite 存任务历史、频道规则、账号状态、日志（重启不丢）
- **凭据加密**：Fernet 加密落盘 token；FloodWait 自动退避；磁盘水位告警；结构化日志 + 轮转
- 多任务并发、`/cancel` 取消、进度节流回写、115 风控退避

> Phase 1-4 已完成：MVP → 性能 → 管理能力（多账号/频道/Web/整理/持久化）→ 健壮性与部署收尾。
> 完整设计见 `/root/.claude/plans/transient-twirling-gizmo.md`。

## 架构

```
TG 消息 → bot/handlers → core/queue → core/pipeline
                                       ├─ core/downloader (stream_media + 内联 SHA1)
                                       └─ core/uploader  → cloud115 (秒传 → OSS)
                                                       → 上传后 core/workspace 清理临时文件
```

## 快速开始

### 0. （国内服务器）先部署代理

TG 在国内服务器上直连不通，需先有代理。项目自带一键部署脚本（mihomo/Clash 内核，
下载二进制 + 订阅配置 + GeoIP + systemd 服务 + 连通性实测）：

```bash
sudo scripts/setup-mihomo.sh <你的订阅地址>     # 或交互式输入；也支持本地 config.yaml 路径
```

装好后在 `config.yaml` 的 `telegram` 段启用（115/OSS 不走代理，国内直连更快）：

```yaml
telegram:
  proxy: "http://127.0.0.1:7890"    # mihomo 默认混合端口；socks5:// 同样支持
```

境外服务器可跳过本节（proxy 留空）。

> Debian/Ubuntu 系统 Python 直接 `pip install` 会被 PEP 668 拦截（`externally-managed-environment`），**必须用 venv**（如上）或 Docker。
> 115 协议为本项目手写（`cloud115/`），**不依赖 p115client/p115oss**，无需任何 GitHub 源安装——这正是抛弃该生态的原因（双 monorepo 依赖互锁，装环境极脆弱）。

### 1. 安装依赖

> **需要 Python ≥3.12**（Docker 镜像已用 `python:3.12-slim`）。pyrogram 原版在 3.12 有兼容问题，故用其维护分支 **pyrofork**（API 一致，仍 `from pyrogram import ...`）。

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # 全部来自 PyPI，无 GitHub 源
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml：填 telegram.api_id/api_hash/bot_token 与 115 账号信息
```

- **Telegram**：到 https://my.telegram.org 申请 `api_id`/`api_hash`；@BotFather 创建 bot 拿 `bot_token`。
- **可选 user session**（下载 >20MB 视频必需）：`python scripts/make_session.py`，再把生成的 `config/user.session` 填入 `telegram.user_session`。

### 3. 首次授权 + 冒烟测试 ⚠️ 重要

115 协议为本项目**手写实现**（`cloud115/openapi.py` 开放平台 + `cloud115/oss_upload.py` OSS 直传），
对照 telegram-115bot 与 p115oss 协议逐字段校准，**不依赖任何 p115 SDK**。

**首次使用先授权**（终端扫码，或启动 bot 后用 `/auth`）：

```bash
python scripts/check115.py --auth    # 终端直接渲染二维码（深色背景扫不动时 QR_INVERT=0）
```

然后跑冒烟测试：

```bash
python scripts/check115.py
```

依次验证：初始化 → 探活 → 列目录 → 建目录 → 首次上传（OSS 直传，含 ≥1MB 二次区间 SHA1 校验）
→ 同内容再传（🎯 秒传命中）→ 全链路。任一步报错，按报错定位：

- 接口字段/鉴权问题 → `cloud115/openapi.py`（路径: `/open/folder/get_info` 等；错误码 40140125 自动刷新）
- OSS 上传问题 → `cloud115/oss_upload.py`（V1 HMAC-SHA1 签名 + multipart + callback）
- 秒传判定 → `cloud115/oss.py`（`data.status == 2`；`sign_key/sign_check` 二次区间校验）

> **秒传/OSS 原理**：115 不返回预签名 URL，而是给 STS 临时凭证，客户端做 OSS V1 HMAC-SHA1
> 签名直传阿里云（单 PUT 或 multipart 分片并发），complete 时带 base64 callback 通知 115 落盘。

TG 侧（并行下载、进度、队列、pipeline、取消）无需改动。

### 4. 运行

```bash
python main.py
```

或 Docker：

```bash
docker compose up -d --build
docker compose logs -f
```

### 5. 使用

在 Telegram 里向你的 bot 发送视频/文件，它会自动下载并上传到 `config.yaml` 里 `upload.target_dir` 指定的 115 目录。

| 命令 | 说明 |
|---|---|
| `/start` `/help` | 帮助 |
| `/setdir <115路径>` | 设置你的 115 目标目录（如 `/tg115bot/movies`） |
| `/auth` | 未授权账号发起 115 扫码授权（发二维码）；已授权则逐个探活报告 |
| `/cancel` | 取消你最近一个进行中的任务 |
| `/channels` | 查看频道监控规则 |
| `/addchannel <频道ID> <目标目录> [关键词...]` | 新增/更新频道规则（关键词为白名单，逗号分隔） |
| `/delchannel <规则ID>` | 删除频道规则 |

### 6. Web 管理台（可选）

`config.yaml` 设 `web.enable: true`（或环境变量 `TG115BOT__WEB__ENABLE=true`）后，启动即提供：

- `http://<host>:<port>/` — 仪表盘（实时进度，每 3s 自动刷新）
- `/tasks` — 任务历史　`/accounts` — 账号状态　`/channels` — 频道规则（在线增删）　`/logs` — 日志

凭据为 `config.web.username` / `config.web.password`（HTTP Basic）。

### 7. 频道监控（可选）

1. 把 bot 加入目标频道（成员或管理员）。
2. `config.yaml` 设 `channel_monitor.enabled: true`。
3. 用 `/addchannel <频道ID> <目标目录> [关键词...]` 或 Web 台添加规则。关键词为白名单（留空=该频道所有媒体）；`config.yaml` 可配黑名单。

频道 ID 可在 TG 内转发消息给 `@userinfobot` 获取。

## 项目结构

```
tg115bot/
├── main.py                # 入口（组装日志/DB/多账号/队列/bot/监控/Web）
├── config.py              # yaml+env 配置
├── bot/                   # Pyrogram 客户端、handlers、频道监控
│   ├── client.py  handlers.py  channel_monitor.py
├── core/                  # 业务核心
│   ├── downloader.py  uploader.py  pipeline.py  queue.py
│   ├── organize.py  workspace.py  progress.py  app.py
├── cloud115/              # 115 手写协议（开放平台+OSS，零 SDK 依赖）
│   ├── openapi.py  oss_upload.py  account.py  client.py  filesystem.py  oss.py
├── persistence/           # SQLite（aiosqlite）任务/规则/账号/日志
│   ├── db.py  models.py
├── web/                   # FastAPI + Jinja2 + HTMX 管理台
│   ├── app.py  auth.py  views.py  templates/
├── utils/                 # rate(限速/退避/FloodWait)  crypto(加密)  logging
├── tests/                 # 纯逻辑测试（organize/匹配/轮转/加密/配置/FloodWait/DB/115协议）
├── scripts/
│   ├── setup-mihomo.sh    # 一键部署 mihomo 代理（国内服务器 TG 必需）
│   ├── check115.py        # 115 冒烟测试（首次必跑）
│   ├── make_session.py    # 生成 user session
│   └── healthcheck.py     # 容器健康检查
├── config.yaml.example  requirements.txt  .env.example
├── Dockerfile  docker-compose.yml
```

## 配置项速览

见 `config.yaml.example` 内注释。常用：

| 键 | 默认 | 说明 |
|---|---|---|
| `upload.target_dir` | `/tg115bot` | 115 默认目标目录 |
| `upload.workers` | 8 | TG 并行下载分片数（>1 启用并行分片下载） |
| `upload.oss_concurrency` | 8 | OSS 分片并发数 |
| `upload.chunk_size` | 1MB | 下载分片 |
| `upload.delete_after_upload` | true | 上传后删本地 |
| `queue.concurrency` | 2 | 同时处理的任务数 |
| `accounts[].app_id` | 空 | 开放平台 AppID（空=内置公共测试 ID）；多账号配多条 + `weight` 权重 |
| `storage.min_free_gb` | 5 | 磁盘水位下限 |
| `organize.rename_template` | `{filename}` | 重命名模板（`{date}/{name}/{ext}` 等） |
| `organize.classify_by_ext` | false | 按扩展名归入子目录 |
| `channel_monitor.enabled` | false | 频道监控开关 |
| `web.enable` | false | Web 管理台开关 |
| `logging.level` | `INFO` | 日志级别（文件轮转 + DB 缓冲） |

环境变量覆盖：`TG115BOT__段__键=值`（双下划线分层，如 `TG115BOT__TELEGRAM__BOT_TOKEN`）。
凭据加密口令：`TG115BOT_SECRET_KEY`（加密落盘的 open_token，见 `.env.example`）。

## 路线图

- [x] **Phase 1** 端到端 MVP：单路下载 + 秒传/OSS 上传 + TG 交互 + Docker
- [x] **Phase 2** 性能：`stream_media(offset)` 多路并行分片下载；秒传探测 + OSS 分片并发；多任务并发；`/cancel` 取消
- [x] **Phase 3** 管理：Web 管理台（FastAPI+HTMX）；频道监控 + 关键词规则；多账号轮转；重命名/归类；SQLite 持久化
- [x] **Phase 4** 健壮性：凭据加密（Fernet）；FloodWait 退避；磁盘水位告警；结构化日志 + 轮转 + DB 落盘；健康检查；Docker compose volume/env

## 风险

- 115 风控：优先 OAuth；请求最小间隔+抖动+指数退避；勿高并发轰炸。
- token 过期：40140125 自动刷新并重试；彻底失效时 `/auth` 重新扫码。
- 大文件：全程流式，不读进内存；预分配 + seek 写盘。

## 免责声明

仅供个人学习与合法的网盘文件管理使用。请遵守当地法律法规与 115 / Telegram 服务条款，使用风险自负。
