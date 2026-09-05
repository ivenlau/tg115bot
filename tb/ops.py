"""流程编排：init / mihomo / session / update / doctor。

init 与 mihomo 委托给仓库里久经实战的脚本（init.sh 的镜像兜底、init.ps1 的
PS 5.1 base64 兼容都在里面），tb 只做跨平台选择与交互式直通；doctor 体检则
纯 Python 实现（探活 / 磁盘 / mihomo 安全校验）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tb import INSTALL_DIR, VERSION

SCRIPTS = INSTALL_DIR / "scripts"
CONFIG_FILE = INSTALL_DIR / "config.yaml"


def _spawn_interactive(cmd: list[str]) -> int:
    """交互式直通子进程（继承 tty，Ctrl+C 直接传给子进程）。"""
    try:
        return subprocess.run(cmd, cwd=str(INSTALL_DIR)).returncode
    except KeyboardInterrupt:
        return 130


def cmd_init() -> int:
    """一键初始化（依赖/代理/配置/扫码授权），幂等可重跑。"""
    if os.name == "posix":
        return _spawn_interactive(["bash", str(SCRIPTS / "init.sh")])
    return _spawn_interactive(["powershell", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPTS / "init.ps1")])


def cmd_mihomo(sub_url: str) -> int:
    """部署/更新 mihomo 代理（仅 Linux；Windows 请自行安装 Clash 并在 init 时填代理地址）。"""
    if os.name != "posix":
        print("[x] Windows 版不做代理部署：请自行安装 Clash/v2rayN，"
              "初始化时把本地监听地址（如 http://127.0.0.1:7890）填进 telegram.proxy")
        return 1
    cmd = ["sudo", "bash", str(SCRIPTS / "setup-mihomo.sh")]
    if sub_url:
        cmd.append(sub_url)
    return _spawn_interactive(cmd)


def cmd_session() -> int:
    """生成 TG user session（提升下载额度，Premium 可下 4GB）。"""
    return _spawn_interactive([sys.executable, str(SCRIPTS / "make_session.py")])


def _git_rev() -> str:
    try:
        r = subprocess.run(["git", "-C", str(INSTALL_DIR), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def cmd_update() -> int:
    """更新代码与依赖（git pull + pip install -r）。"""
    if not (INSTALL_DIR / ".git").exists():
        print("[x] 非 git 安装（可能是 zip 解压），请手动更新后重跑 tb init")
        return 1
    print("[+] 拉取最新代码…")
    r = subprocess.run(["git", "-C", str(INSTALL_DIR), "pull", "--ff-only"])
    if r.returncode != 0:
        print("[x] git pull 失败（本地有改动？可 git stash 后重试）")
        return 1
    print("[+] 刷新依赖…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        str(INSTALL_DIR / "requirements.txt")])
    if r.returncode != 0:
        print("[!] 依赖刷新失败，可稍后手动: pip install -r requirements.txt")
    print("[+] 更新完成。若服务在跑，请: tb restart")
    return 0


def cmd_version() -> int:
    print(f"tb {VERSION} (rev {_git_rev()})")
    print(f"  安装目录: {INSTALL_DIR}")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    return 0


# ── 配置编辑（TUI 配置页；路径参数化便于测试，默认真实 config.yaml） ─────

def validate_config_text(text: str) -> tuple[bool, str]:
    """双重校验，不落盘：YAML 语法 → pydantic 模型（AppConfig）。"""
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return False, f"YAML 语法错误: {e}"
    if not isinstance(data, dict):
        return False, "顶层结构应为键值映射（段名: 值）"
    try:
        from config import AppConfig
        AppConfig(**data)
    except Exception as e:  # noqa: BLE001 -- pydantic ValidationError
        return False, f"配置校验失败: {e}"
    return True, "OK"


def write_config_text(text: str, path: Path | None = None) -> Path:
    """全文写回（先备份 config.yaml.bak.<时间戳>）。调用前应已 validate 通过。

    写用户看到的原文而非 yaml 重导出——保留注释与排版，零丢失。
    """
    p = path or CONFIG_FILE
    if p.exists():
        bak = p.with_name(p.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(p, bak)
    if not text.endswith("\n"):
        text += "\n"
    p.write_text(text, encoding="utf-8")
    return p


def set_config_key(dotted: str, value, path: Path | None = None) -> tuple[bool, str]:
    """改一个键：yaml 往返（保键序/中文/未知键）+ pydantic 校验后落盘。

    开关类快捷修改用；校验失败不落盘。与 manual.py/init.ps1 的写法同族。
    """
    import yaml
    p = path or CONFIG_FILE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as e:
        return False, f"读取失败: {e}"
    if not isinstance(data, dict):
        return False, "现有配置顶层不是键值映射，请改用编辑器整文修改"
    node = data
    keys = dotted.split(".")
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value
    try:
        from config import AppConfig
        AppConfig(**data)
    except Exception as e:  # noqa: BLE001
        return False, f"校验失败（未落盘）: {e}"
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return True, "OK"


# ── doctor：一键体检 ────────────────────────────────────────────────────

def _read_mihomo_cfg(path: Path | None = None) -> dict[str, str]:
    """从 mihomo 配置抄几个关键标量键（够用即可，不上 YAML 解析器）。"""
    out: dict[str, str] = {}
    cfg = path or Path("/etc/mihomo/config.yaml")
    if not cfg.exists():
        return out
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(mixed-port|port|socks-port|external-controller|allow-lan):\s*(\S+)", line)
            if m:
                out.setdefault(m.group(1), m.group(2).strip("'\""))
    except OSError:
        pass
    return out


def _mihomo_warnings() -> list[str]:
    """安全校验，逻辑对齐 init.sh 的 mihomo_security_check。"""
    mi = _read_mihomo_cfg()
    if not mi:
        return []  # 未部署（可直连 TG 时不需要）
    warns: list[str] = []
    ctrl = mi.get("external-controller", "")
    if ctrl and not ctrl.startswith(("127.0.0.1", "localhost")):
        warns.append(f"mihomo 控制 API 绑定在 {ctrl} —— 公网可远程改你的代理配置！"
                     "修复: /etc/mihomo/config.yaml 改 external-controller: 127.0.0.1:9090")
    port = mi.get("mixed-port") or mi.get("port") or mi.get("socks-port") or "7890"
    body = Path("/etc/mihomo/config.yaml").read_text(encoding="utf-8", errors="replace")
    listen = ""
    try:
        r = subprocess.run(["ss", "-tln"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3].endswith(f":{port}"):
                listen = parts[3]
                break
    except (OSError, subprocess.SubprocessError):
        return warns  # 无 ss（如 WSL1/老系统）则跳过监听检查
    if listen and not listen.startswith(("127.0.0.1", "localhost", "[::1]")):
        m = re.search(r"^lan-allowed-ips:(.*?)(?=^\S|\Z)", body,
                      re.MULTILINE | re.DOTALL)
        block = m.group(1) if m else ""
        if not block or "0.0.0.0/0" in block:
            warns.append(f"mihomo 代理端口监听 {listen} 且缺源 IP 白名单 —— 公网开放代理，"
                         "会被扫描滥用！修复: 重跑 sudo bash scripts/setup-mihomo.sh <订阅>")
    return warns


def doctor_checks() -> list[tuple[str, bool, str]]:
    """逐项体检：返回 [(名称, ok, 详情)]。CLI（cmd_doctor 打印）与 TUI 共用。

    阻塞数秒（含 check115 --probe 子进程与磁盘/网络探测），调用方放线程。
    """
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python", sys.version_info >= (3, 12), sys.version.split()[0]))

    has_cfg = (INSTALL_DIR / "config.yaml").exists()
    checks.append(("config.yaml", has_cfg, "存在" if has_cfg else "缺失（先跑 tb init）"))

    # 115 探活（委托 check115 --probe，轻量不产生上传）。
    # 子进程管道强制 UTF-8：中文 Windows 上管道 stdout 默认 GBK，脚本打印的
    # ✅/❌ 编不出会 UnicodeEncodeError 崩掉（诊断误报「授权失败」）；父进程
    # 解码同步显式 UTF-8，errors=replace 兜底任何杂字节。
    if has_cfg:
        r = subprocess.run([sys.executable, str(SCRIPTS / "check115.py"), "--probe"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        probe_out = (r.stdout or r.stderr).strip().splitlines()
        detail = probe_out[-1] if probe_out else f"exit {r.returncode}"
        checks.append(("115 授权", r.returncode == 0, detail))

    try:
        free_gb = shutil.disk_usage(str(INSTALL_DIR)).free / 1024 ** 3
        checks.append(("磁盘", free_gb >= 5, f"剩余 {free_gb:.1f}GB"))
    except OSError as e:
        checks.append(("磁盘", False, str(e)))

    if os.name == "posix":
        warns = _mihomo_warnings()
        if warns:
            checks.extend(("mihomo 安全", False, w) for w in warns)
        else:
            checks.append(("mihomo 安全", True, "无暴露风险（未部署或已加固）"))

    from tb import service
    pid = service.live_pid()
    checks.append(("服务", True, f"运行中 PID {pid}" if pid else "未运行（tb start 启动）"))
    return checks


def cmd_doctor() -> int:
    """一键体检：环境 / 配置 / 115 探活 / 磁盘 / mihomo 安全 / 服务。"""
    print(f"tb doctor (rev {_git_rev()})")
    print("─" * 56)
    checks = doctor_checks()
    ok = True
    for name, fine, detail in checks:
        ok = ok and fine
        print(f"  {'✅' if fine else '❌'} {name}: {detail}")
    print("─" * 56)
    print("结论: " + ("一切正常 ✅" if ok else "存在需要处理的项目 ❌（见上）"))
    return 0 if ok else 1
