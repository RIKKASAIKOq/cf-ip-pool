#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF IP Pool - 定时任务
====================

把「跑一次 IP 池构建」注册成系统定时任务，之后到点自己跑，
不用手敲命令，也不用挂个进程死等。

    python scripts/schedule.py install          # 按默认每 6 小时跑一次
    python scripts/schedule.py status           # 看装没装、跑的什么命令
    python scripts/schedule.py uninstall        # 卸掉

装的是什么：到点执行 `python main.py -y`，也就是完整那套
预检推送配置 -> 生成 ipv4.txt -> 按 gh_config.json 的 auto_push 推送。
想让它只跑不推，install 时加 --no-push。

跨平台：
    Windows           任务计划程序 schtasks
    Linux / macOS     crontab
    Termux / Android  crontab（要先 pkg install cronie，见下面的提示）
"""

import argparse
import json
import locale
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根
MAIN_PY = os.path.join(HERE, "main.py")
SCHEDULE_PY = os.path.join(HERE, "scripts", "schedule.py")
LOG_REL = os.path.join("logs", "schedule.log")

TASK_NAME = "CF-IP-Pool"
BEGIN = "# >>> cf-ip-pool schedule >>>"
END = "# <<< cf-ip-pool schedule <<<"

SUBS = ("install", "uninstall", "remove", "rm", "status", "show", "ls", "run")

DEFAULT_INTERVAL = 360        # 6 小时，跟 Actions 的 cron 对齐
GH_CONFIG_NAME = "gh_config.json"   # 定时设置的来源文件（被 .gitignore 忽略）

SCHEDULE_KEYS = ("interval", "at", "name", "no_push")   # gh_config.json 的 "schedule" 段里认这几个键
MAX_DAILY_STEP = 30           # cron / schtasks 的「每 N 天」上限


# ─── 小工具 ───────────────────────────────────────────────────────────────


def log(msg=""):
    print(msg, flush=True)


def _run(cmd, stdin_text=None):
    """跑子进程，抓输出。返回 (rc, 文本)。
    Windows 上那些工具（schtasks/crontab）按本地代码页输出，按字节收再解码，
    免得中文变成乱码。
    """
    try:
        data = stdin_text.encode("utf-8") if stdin_text is not None else None
        p = subprocess.run(cmd, input=data, capture_output=True, cwd=HERE, timeout=120)
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 126, str(e)
    enc = "utf-8"
    if os.name == "nt":
        try:
            enc = locale.getpreferredencoding(False) or "mbcs"
        except Exception:
            enc = "mbcs"
    out = (p.stdout or b"").decode(enc, "replace") + (p.stderr or b"").decode(enc, "replace")
    return p.returncode, out


def load_schedule_cfg(path=None):
    """
    读 gh_config.json 里的 "schedule" 对象，当定时任务的默认值。

    为什么是嵌套对象而不是顶层键：gh_config.json 顶层已经有个 interval，
    但那是 build_pool 的「循环间隔秒数」（loop>1 时用），和这里的「间隔分钟数」
    语义相反，混在一起会被误读。

    找不到文件 / 没有 schedule 段 / 那段不是对象，都返回 {}（等于全用默认值）。
    """
    if path:
        cands = [path]
    else:
        cands = [os.path.join(os.getcwd(), GH_CONFIG_NAME),
                 os.path.join(HERE, GH_CONFIG_NAME)]
    for p in cands:
        if os.path.exists(p):
            break
    else:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.loads(f.read().lstrip("\ufeff"))
    except Exception as e:
        log(f"  ! {p} 读取失败: {e}（定时设置改用命令行/默认值）")
        return {}
    if not isinstance(raw, dict):
        log(f"  ! {p} 顶层不是对象，定时设置改用命令行/默认值")
        return {}
    sched = raw.get("schedule")
    if sched is None:
        return {}
    if not isinstance(sched, dict):
        log(f"  ! {p} 的 schedule 段不是对象，已忽略")
        return {}
    unknown = [k for k in sched if not k.startswith("_") and k not in SCHEDULE_KEYS]
    if unknown:
        log(f"  {p} schedule 段忽略不认识的键: {', '.join(unknown)}")
    return {k: v for k, v in sched.items() if k in SCHEDULE_KEYS}


def detect_platform():
    if os.name == "nt" or sys.platform.startswith("win"):
        return "win"
    try:
        u = os.uname().sysname.lower()
    except AttributeError:
        return "posix"
    if u.startswith("linux"):
        prefix = os.environ.get("PREFIX", "")
        return "termux" if "com.termux" in prefix else "linux"
    if u.startswith("darwin"):
        return "mac"
    return "posix"


def build_argv(no_push=False, extra=None):
    """定时任务到点要执行的那条命令。用绝对路径，不依赖 cwd 和 PATH。"""
    argv = [sys.executable, MAIN_PY, "-y"]
    if no_push:
        argv.append("--no-push")
    if extra:
        argv += [a for a in extra if a]
    return argv


def sh_line(argv):
    """Shell 里能直接跑的一行。Windows 用 cmd 的规则，posix 用 sh 的规则。"""
    if os.name == "nt":
        return win_line(argv)
    return " ".join(shlex.quote(a) for a in argv)


def win_line(argv):
    """schtasks /TR 要的单个字符串（Windows 命令行规则）。"""
    return subprocess.list2cmdline(argv)


def parse_at(text):
    """--at 支持 HH:MM 或纯分钟数。返回 (minute, hour)。"""
    s = str(text).strip()
    try:
        if ":" in s:
            h, m = s.split(":", 1)
            minute, hour = int(m), int(h)
        else:
            minute, hour = int(s), 0
    except ValueError:
        raise ValueError(f"--at 看不懂: {text}（要 HH:MM 或者分钟数）")
    if not (0 <= minute <= 59) or not (0 <= hour <= 23):
        raise ValueError(f"--at 超出范围: {text}（分钟 0-59，小时 0-23）")
    return minute, hour


# ─── 间隔翻译 ──────────────────────────────────────────────────────────────


def cron_spec(interval, minute=0, hour=0):
    """把「每 interval 分钟」翻成 cron 的五段。表达不了就抛 ValueError。"""
    if interval < 1:
        raise ValueError("间隔至少要 1 分钟")
    if interval < 60:
        return f"*/{interval} * * * *"
    if interval % 60 == 0 and interval // 60 <= 23:
        return f"{minute} */{interval // 60} * * *"
    if interval % 1440 == 0 and interval // 1440 <= MAX_DAILY_STEP:
        return f"{minute} {hour} */{interval // 1440} * *"
    raise ValueError(
        f"cron 表达不了「每 {interval} 分钟」。可用的档位：1-59 分钟、"
        f"整小时（最多 23 小时）、整天数（最多 {MAX_DAILY_STEP} 天）")


def schtasks_sched(interval):
    """把「每 interval 分钟」翻成 schtasks 的 /SC /MO 组合。"""
    if interval < 1:
        raise ValueError("间隔至少要 1 分钟")
    if interval < 60:
        return ["/SC", "MINUTE", "/MO", str(interval)]
    if interval % 60 == 0 and interval // 60 <= 23:
        return ["/SC", "HOURLY", "/MO", str(interval // 60)]
    if interval % 1440 == 0 and interval // 1440 <= MAX_DAILY_STEP:
        return ["/SC", "DAILY", "/MO", str(interval // 1440)]
    raise ValueError(
        f"任务计划程序表达不了「每 {interval} 分钟」。可用的档位：1-59 分钟、"
        f"整小时（最多 23 小时）、整天数（最多 {MAX_DAILY_STEP} 天）")


def describe_interval(interval):
    """给人看的一句「多久跑一次」。

    和 cron_spec / schtasks_sched 用同一套判据，保证说出来的就是装出来的。
    """
    if interval < 60:
        return f"每 {interval} 分钟"
    hours = interval // 60
    if interval % 60 == 0 and hours <= 23:
        return f"每 {hours} 小时"
    if interval % 1440 == 0 and interval // 1440 <= MAX_DAILY_STEP:
        return f"每 {interval // 1440} 天"
    return f"每 {interval} 分钟"


# ─── cron（Linux / macOS / Termux） ────────────────────────────────────────


def cron_missing_hints(plat):
    log("  ! 找不到 crontab 命令")
    if plat == "termux":
        log("    Termux 装一下再用：")
        log("        pkg install cronie")
        log("        crond            # 启动；想开机自起跑 sv-enable crond")
        log("        termux-wake-lock # 建议开着，否则系统可能杀掉计划任务")
    elif plat == "mac":
        log("    macOS 自带 crontab；若被拦，去 系统设置 -> 隐私与安全性 -> 完全磁盘访问权限 放行终端")
    else:
        log("    Debian/Ubuntu : sudo apt install cron")
        log("    Fedora        : sudo dnf install cronie && sudo systemctl enable --now crond")
        log("    Alpine        : apk add dcron && rc-update add crond")


def cron_read():
    """读当前 crontab，返回行列表。没有或读不到都算空。"""
    rc, out = _run(["crontab", "-l"])
    if rc != 0:
        return [], out
    lines = [l.rstrip() for l in out.replace("\r\n", "\n").split("\n")]
    return [l for l in lines if l.strip() != ""], ""


def cron_write(lines):
    if not lines:
        return _run(["crontab", "-r"])
    return _run(["crontab", "-"], stdin_text="\n".join(lines) + "\n")


def cron_strip(lines):
    """去掉我们自己那一段，别人的 crontab 一行不碰。"""
    keep, inside = [], False
    for l in lines:
        s = l.strip()
        if s == BEGIN:
            inside = True
            continue
        if s == END:
            inside = False
            continue
        if not inside:
            keep.append(l)
    return keep


def cron_install(spec, desc, argv, name, dry=False):
    plat = detect_platform()
    if not _has("crontab"):
        cron_missing_hints(plat)
        return 127

    log_path = os.path.join(HERE, LOG_REL)
    entry = f"{spec} {sh_line(argv)} >> {shlex.quote(log_path)} 2>&1"

    lines, err = cron_read()
    lines = cron_strip(lines)
    lines.append(BEGIN)
    lines.append(f"# task: {name}   ({desc})")
    lines.append(entry)
    lines.append(END)

    log(f"  平台      : {plat}")
    log(f"  计划      : {spec}")
    log(f"  命令      : {sh_line(argv)}")
    log(f"  日志      : {log_path}")
    if dry:
        log("  [SKIP] --dry-run，没有真的写 crontab")
        log("  会写进去的内容：")
        for l in lines[-4:]:
            log("      " + l)
        return 0

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    rc, out = cron_write(lines)
    if rc != 0:
        log(f"  [FAIL] 写 crontab 失败: {out.strip()}")
        return rc
    log("  [ OK ] 已写入 crontab")
    if plat == "termux":
        log("    Termux 注意：确认 cron 在跑（crond / sv status crond），并 termux-wake-lock")
    elif plat == "mac":
        log("    macOS 注意：cron 需要终端的「完全磁盘访问权限」，否则可能不执行")
    return 0


def cron_uninstall(name, dry=False):
    if not _has("crontab"):
        log("  ! 没有 crontab，没什么可卸的")
        return 0
    lines, err = cron_read()
    if not any(l.strip() == BEGIN for l in lines):
        log("  [SKIP] crontab 里没有本项目的定时任务")
        return 0
    left = cron_strip(lines)
    if dry:
        log("  [SKIP] --dry-run，没有真的改 crontab")
        return 0
    rc, out = cron_write(left)
    if rc != 0:
        log(f"  [FAIL] 卸不掉: {out.strip()}")
        return rc
    log("  [ OK ] 已从 crontab 移除")
    return 0


def cron_status(name):
    if not _has("crontab"):
        cron_missing_hints(detect_platform())
        return 1
    lines, err = cron_read()
    block = []
    inside = False
    for l in lines:
        s = l.strip()
        if s == BEGIN:
            inside = True
            block.append(l)
            continue
        if s == END:
            block.append(l)
            inside = False
            continue
        if inside:
            block.append(l)
    if not block:
        log(f"  [SKIP] crontab 里没有本项目的定时任务（要装: install）")
        return 1
    log("  [ OK ] 已安装（crontab）")
    for l in block:
        log("      " + l)
    return 0

# ─── schtasks（Windows） ───────────────────────────────────────────────────


def win_install(sched, at, argv, name, dry=False):
    if not _has("schtasks"):
        log("  ! 找不到 schtasks，换 Linux / macOS / Termux 那份脚本跑")
        return 127
    cmd = ["schtasks", "/Create", "/TN", name, "/TR", win_line(argv), "/F"] + sched
    if at:
        cmd += ["/ST", at]
    log(f"  平台      : Windows（任务计划程序）")
    log(f"  计划      : {' '.join(sched)}" + (f"  首次 {at}" if at else ""))
    log(f"  命令      : {win_line(argv)}")
    if dry:
        log("  [SKIP] --dry-run，没有真的建任务")
        log("  会执行的是: " + " ".join(cmd))
        return 0
    rc, out = _run(cmd)
    if rc != 0:
        log(f"  [FAIL] 建任务失败: {out.strip()}")
        low = out.lower()
        if "access is denied" in low or "拒绝访问" in out:
            log("    hint: 用管理员身份开终端再试")
        return rc
    log("  [ OK ] 已创建任务，细节在「任务计划程序」里能看到")
    return 0


def win_uninstall(name, dry=False):
    if not _has("schtasks"):
        log("  ! 没有 schtasks，没什么可卸的")
        return 0
    rc, out = _run(["schtasks", "/Query", "/TN", name])
    if rc != 0:
        log("  [SKIP] 没有这个任务")
        return 0
    if dry:
        log("  [SKIP] --dry-run，没有真的删")
        return 0
    rc, out = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    if rc != 0:
        log(f"  [FAIL] 删不掉: {out.strip()}")
        return rc
    log("  [ OK ] 已删除任务")
    return 0


def win_status(name):
    if not _has("schtasks"):
        log("  ! 没有 schtasks")
        return 1
    rc, out = _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"])
    if rc != 0:
        log(f"  [SKIP] 没有装这个任务（要装: install）")
        return 1
    log("  [ OK ] 已安装（任务计划程序）")
    keep = ("Task To Run", "任务要运行", "Schedule Type", "计划类型",
            "Next Run Time", "下次运行时间", "Last Run Time", "上次运行时间",
            "Last Result", "上次结果", "Status", "状态", "Run As User", "作为用户运行")
    for line in out.splitlines():
        s = line.strip()
        if any(s.startswith(k) for k in keep):
            log("      " + s)
    return 0


def _has(cmd):
    from shutil import which
    return which(cmd) is not None

# ─── 子命令 ────────────────────────────────────────────────────────────────


def cmd_install(args):
    plat = detect_platform()
    try:
        minute, hour = parse_at(args.at) if args.at else (0, 0)
        if plat == "win":
            sched = schtasks_sched(args.interval)
            at = f"{hour:02d}:{minute:02d}" if args.at else None
        else:
            spec = cron_spec(args.interval, minute, hour)
    except ValueError as e:
        log(f"  [FAIL] {e}")
        return 2

    desc = describe_interval(args.interval)
    argv = build_argv(args.no_push, args.extra)
    log()
    log(f"定时任务 install   [{plat}]   {desc}")
    log()
    if plat == "win":
        return win_install(sched, at, argv, args.name, dry=args.dry_run)
    return cron_install(spec, desc, argv, args.name, dry=args.dry_run)


def cmd_uninstall(args):
    plat = detect_platform()
    log()
    log(f"定时任务 uninstall   [{plat}]   {args.name}")
    log()
    if plat == "win":
        return win_uninstall(args.name, dry=args.dry_run)
    return cron_uninstall(args.name, dry=args.dry_run)


def cmd_status(args):
    plat = detect_platform()
    log()
    log(f"定时任务 status   [{plat}]   {args.name}")
    log()
    if plat == "win":
        return win_status(args.name)
    return cron_status(args.name)


def cmd_run(args):
    """手动跑一次定时任务要跑的那条命令，用来试通不通。"""
    argv = build_argv(args.no_push, args.extra)
    log("  $ " + sh_line(argv))
    try:
        return subprocess.run(argv, cwd=HERE).returncode
    except FileNotFoundError as e:
        log(f"  ! 起不来: {e}")
        return 127
    except KeyboardInterrupt:
        log("  ! 被 Ctrl+C 打断")
        return 130


def main():
    ap = argparse.ArgumentParser(
        prog="schedule.py",
        description="CF IP Pool 定时任务：install / status / uninstall",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="install / run 后面多给的参数（--target 30 之类）会原样进到定时命令里。",
    )
    ap.add_argument("--interval", "--every", type=int, default=None, dest="interval",
                    help=f"间隔分钟数。不给则用 gh_config.json 的 schedule.interval，再不然 {DEFAULT_INTERVAL}（6 小时）")
    ap.add_argument("--at", type=str, default=None,
                    help="首次运行时刻 HH:MM（也可只给分钟数）。不给则用配置里的 schedule.at")
    ap.add_argument("--name", type=str, default=None,
                    help=f"任务名。不给则用配置里的 schedule.name，再不然 {TASK_NAME}")
    ap.add_argument("--no-push", dest="no_push", action="store_true", default=None,
                    help="定时跑的时候不要推送，只生成 ipv4.txt（覆盖配置里的 schedule.no_push）")
    ap.add_argument("--push", dest="no_push", action="store_false",
                    help="定时跑的时候照样推送（覆盖配置里的 schedule.no_push）")
    ap.add_argument("--gh-config", type=str, default=None, metavar="PATH",
                    help="定时设置所在的配置文件（默认在当前目录和仓库根找 gh_config.json）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印会做什么，不动系统")
    sub = ap.add_subparsers(dest="what")

    p_ins = sub.add_parser("install", help="注册定时任务")
    p_ins.add_argument("extra", nargs=argparse.REMAINDER,
                      help="透传给 main.py 的额外参数，例: --target 30")
    sub.add_parser("uninstall", aliases=["remove", "rm"], help="删掉定时任务")
    sub.add_parser("status", aliases=["show", "ls"], help="看装没装、跑什么命令")
    p_rn = sub.add_parser("run", help="手动跑一次定时任务要跑的命令")
    p_rn.add_argument("extra", nargs=argparse.REMAINDER,
                      help="透传给 main.py 的额外参数")

    # argparse 不会跨子命令边界认顶层参数：先把第一个子命令词找出来，
    # 只让顶层解析它前面那段（--interval / --at / --name / --dry-run / --no-push），
    # 子命令之后的参数手动塞给 extra，由 build_argv 原样带进定时命令。
    argv_in = sys.argv[1:]
    cut = next((i for i, a in enumerate(argv_in) if a in SUBS), None)
    if cut is None:
        args = ap.parse_args(argv_in)
    else:
        args = ap.parse_args(argv_in[:cut] + [argv_in[cut]])
        rest = argv_in[cut + 1:]
        if args.what in ("install", "run"):
            args.extra = rest

    if not args.what:
        ap.print_help()
        return 2
    # ── 定时设置：命令行 > gh_config.json 的 schedule 段 > 内置默认 ──
    # argparse 那几项默认值都设成了 None，才好区分「用户没给」和「给的就是 None」。
    sched_cfg = load_schedule_cfg(args.gh_config)
    src = {}

    interval = args.interval
    if interval is None:
        interval = sched_cfg.get("interval")
        src["interval"] = "gh_config.json" if interval is not None else "默认"
    else:
        src["interval"] = "命令行"
    if interval is None:
        interval = DEFAULT_INTERVAL
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        log(f"  [FAIL] schedule.interval 不是整数: {interval!r}")
        return 2
    if interval < 1:
        log(f"  [FAIL] schedule.interval 至少要 1 分钟，给的是 {interval}")
        return 2

    at = args.at
    if at is None:
        at = sched_cfg.get("at")
        src["at"] = "gh_config.json" if at is not None else "默认"
    else:
        src["at"] = "命令行"
    if at is not None:
        try:
            parse_at(at)
        except ValueError as e:
            log(f"  [FAIL] {e}")
            return 2

    name = args.name
    if name is None:
        name = sched_cfg.get("name")
        src["name"] = "gh_config.json" if name is not None else "默认"
    else:
        src["name"] = "命令行"
    if not name:
        name = TASK_NAME
    if sched_cfg:
        log(f"  定时设置来自 gh_config.json 的 schedule 段: {', '.join(sorted(sched_cfg))}")
    log(f"  interval : {interval} 分钟（{src['interval']}）   {describe_interval(interval)}")
    log(f"  at       : {at or '00:00'}（{src['at']}）")
    log(f"  name     : {name}（{src['name']}）")

    no_push = args.no_push
    if no_push is None:
        no_push = bool(sched_cfg.get("no_push", False))
        src["no_push"] = "gh_config.json" if "no_push" in sched_cfg else "默认"
    else:
        src["no_push"] = "命令行"
    log(f"  推送     : {'不推送（只生成 ipv4.txt）' if no_push else '按 auto_push 推送'}（{src['no_push']}）")
    log()

    args.interval, args.at, args.name, args.no_push = interval, at, name, no_push
    if not os.path.exists(MAIN_PY):
        log(f"  [FAIL] 找不到 {MAIN_PY}")
        return 1

    if args.what == "install":
        return cmd_install(args)
    if args.what in ("uninstall", "remove", "rm"):
        return cmd_uninstall(args)
    if args.what in ("status", "show", "ls"):
        return cmd_status(args)
    if args.what == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)