#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF IP Pool — Win / Linux / Termux 一键入口
=========================

一条命令跑完整个流程::

    python main.py

流程（先在自己机器上跑出来，再决定推不推）：
  1. 检查推送配置 gh_config.json：没有就提醒，首次启动可以直接生成模板
  2. 跑 IP 生成：拉官方网段 -> 拆 /24 采样 -> TCP 探测 -> 写 ipv4.txt 到本地
  3. 推送配置齐全时，按 gh_config.json 里的 auto_push 自动提交并推送到 GitHub

放在 GitHub 上跑的那份不走这里：workflow 直接调 scripts/build_pool.py --config config.json，
用 secrets.GITHUB_TOKEN 提交推送（见 .github/workflows/build.yml）。

main.py 自己的参数（其余参数原样传给 scripts/build_pool.py）：
  --init          生成 gh_config.json 模板后退出（首次启动用）
  --check         只跑推送链路自检（scripts/check_push.py），不构建
  --schedule      管理定时任务（install / status / uninstall），不构建
  --config PATH   指定运行配置；不给 = 有 gh_config.json 就用它，否则用 config.json
  --gh-config P   推送配置路径（默认 gh_config.json）
  -y, --yes       非交互，提问一律答「是」
  --no-push       本次不推送（透传给 build_pool）
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
BUILD_POOL = os.path.join(SCRIPTS, "build_pool.py")
CHECK_PUSH = os.path.join(SCRIPTS, "check_push.py")
SCHEDULE = os.path.join(SCRIPTS, "schedule.py")

# 复用 build_pool 里已经写好的配置/推送逻辑，避免两套实现漂移
sys.path.insert(0, SCRIPTS)
import build_pool as bp  # noqa: E402

GH_CONFIG = bp.GH_CONFIG_NAME          # "gh_config.json"
RUN_CONFIG = "config.json"             # Actions 跑的那份（生产口径）

BAR = "=" * 64


def log(msg=""):
    print(msg, flush=True)


def rule(ch="─"):
    log(ch * 64)


def ask(text, default=True, assume_yes=False):
    """问一个是/否。非交互环境（管道/CI）用 assume_yes 顶替，不卡住。"""
    if not sys.stdin.isatty() or assume_yes:
        return assume_yes or default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{text} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log()
        return default
    if not ans:
        return default
    return ans in ("y", "yes", "是", "true", "1")


def _run(cmd):
    """跑子进程，输出直接透传到终端。返回退出码。"""
    log("  $ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=HERE).returncode
    except FileNotFoundError as e:
        log(f"  ! 起不来: {e}")
        return 127
    except KeyboardInterrupt:
        log("  ! 被 Ctrl+C 打断")
        return 130


# ─── 1. 推送配置预检 ───────────────────────────────────────────────────────────

def preflight_push(gh_path, assume_yes=False):
    """
    检查 gh_config.json：在不在、齐不齐、是不是还是 your_ 占位符。
    返回 (push_ready, note)。首次启动没有这份文件时，会问要不要直接生成模板。
    """
    log()
    rule()
    log("推送配置预检")
    rule()

    found = None
    for cand in bp._gh_config_candidates(gh_path, RUN_CONFIG):
        if os.path.exists(cand):
            found = cand
            break

    # 首次启动：没有推送配置 -> 提醒 + 可以顺手生成模板
    if found is None:
        log(f"  ! 没找到推送配置（找过: {', '.join(bp._gh_config_candidates(gh_path, RUN_CONFIG))}）")
        log("    没有它也能在自己机器上跑、生成 ipv4.txt，只是没法推到 GitHub。")
        log("    首次启动可以现在生成一份模板：把 your_ 开头的值换成真实值就能用。")
        if ask("    要现在生成 gh_config.json 吗？", default=True, assume_yes=assume_yes):
            rc = _run([sys.executable, BUILD_POOL, "--init-gh-config"] + (["--gh-config", gh_path] if gh_path else []))
            if rc == 0:
                log("    已生成。接下来：编辑 gh_config.json 填 token / 用户名 / 仓库 / 分支，")
                log("    然后跑 python scripts/check_push.py 验一遍通不通。")
            return False, "推送配置缺失，本次只生成不推送"
        log("    跳过。想生成时跑: python main.py --init")
        return False, "推送配置缺失，本次只生成不推送"

    log(f"  [ OK ] 推送配置: {found}")
    try:
        with open(found, "r", encoding="utf-8") as f:
            raw = __import__("json").loads(f.read().lstrip("\ufeff"))
    except Exception as e:
        log(f"  ! 读取失败: {e}")
        return False, f"推送配置读不出来（{e}）"

    ok, problems = bp._check_push_config(raw)
    for p in problems:
        log(f"  ! {p}")
    if not ok:
        log("    hint: your_xxx 是占位符，不会被当真；填好真实值再推")
        log("          只想要结果不推送的话不用管，接着往下跑就行")
        return False, "推送配置还是占位符，本次只生成不推送"

    token = bp._resolve_token(raw)
    repo = bp._resolve_repo(raw)
    branch = bp._resolve_branch(raw)
    if not token:
        log("  [WARN] 没配 token（也没设 GITHUB_TOKEN / GH_TOKEN）")
        log("         会退回本机 git 凭据；想用 PAT 就填 gh_config.json 的 github_token")
    else:
        log(f"  [ OK ] token: {bp._mask('x-access-token:' + token).split('@')[0]}…  ({len(token)} chars)")
    if not repo:
        log("  ! 定不下来推送目标：写 github_username + repo_name，或确认 origin 指得对")
        return False, "推送目标定不下来，本次只生成不推送"
    log(f"  [ OK ] 推送目标: {repo} @ {branch or '(当前分支)'}")
    log(f"  [ OK ] auto_push: {raw.get('auto_push', False)}"
        + ("" if raw.get("auto_push") else "   （想跑完自动推就改成 true，或加 --push）"))
    return True, ""


# ─── 2. 构建 ─────────────────────────────────────────────────────────────────

def pick_run_config(given_config):
    """运行配置：显式给了就听你的；否则有 gh_config.json 就自己机器上的小口径，没有就生产口径。"""
    if given_config:
        return given_config, "命令行指定"
    if os.path.exists(os.path.join(HERE, GH_CONFIG)):
        return GH_CONFIG, "自己机器上的配置（小样本 + 推送）"
    return RUN_CONFIG, "仓库默认（生产口径，Actions 同款）"


def build(run_cfg, passthrough):
    log()
    rule()
    log(f"开始构建 IP 池   运行配置: {run_cfg}")
    rule()
    cmd = [sys.executable, BUILD_POOL, "--config", run_cfg] + passthrough
    return _run(cmd)


# ─── 收尾 ────────────────────────────────────────────────────────────────────

def finish(push_ready, note, run_cfg):
    log()
    rule()
    if push_ready:
        log("跑完了。auto_push 开着的话上面已经推过；想确认链路通不通跑:")
        log("    python scripts/check_push.py")
    else:
        log("跑完了，ipv4.txt 已写在本地。")
        if note:
            log(f"    （{note}）")
        log("想推到 GitHub 的话:")
        log("    1. python main.py --init      # 没有 gh_config.json 时生成模板")
        log("    2. 编辑 gh_config.json，把 your_ 开头的值换成真实值")
        log("    3. python scripts/check_push.py   # 先验链路，不真推")
        log("    4. python main.py             # auto_push=true 时跑完自动推")
    log(f"    （本次运行配置: {run_cfg}）")
    rule()


# ─── 入口 ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="CF IP Pool Win/Linux/Termux 一键入口：预检推送配置 -> 生成 IP 池 -> 按需推送",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="其余参数（--target/--workers/--outdir/--no-push...）原样传给 scripts/build_pool.py",
    )
    ap.add_argument("--init", action="store_true",
                    help=f"生成 {GH_CONFIG} 模板后退出（首次启动）")
    ap.add_argument("--check", action="store_true",
                    help="只跑推送链路自检（scripts/check_push.py），不构建")
    ap.add_argument("--config", type=str, default=None,
                    help=f"运行配置；不给 = 有 {GH_CONFIG} 用它，否则用 {RUN_CONFIG}")
    ap.add_argument("--gh-config", type=str, default=None,
                    help=f"推送配置路径（默认 {GH_CONFIG}）")
    ap.add_argument("--schedule", action="store_true",
                    help="管理定时任务（install / status / uninstall），其余参数传给 scripts/schedule.py")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="非交互，提问一律答是")
    args, passthrough = ap.parse_known_args()

    # --init：生成推送配置模板
    if args.init:
        cmd = [sys.executable, BUILD_POOL, "--init-gh-config"]
        if args.gh_config:
            cmd += ["--gh-config", args.gh_config]
        rc = _run(cmd)
        if rc == 0:
            log()
            log("接下来：编辑 gh_config.json 填 token / 用户名 / 仓库 / 分支，")
            log("        然后 python scripts/check_push.py 验一遍通不通。")
        return rc

    # --check：只验推送链路
    if args.check:
        cmd = [sys.executable, CHECK_PUSH]
        if args.gh_config:
            cmd += ["--config", args.gh_config]
        cmd += passthrough
        return _run(cmd)

    # --schedule：装/查/卸系统定时任务
    if args.schedule:
        return _run([sys.executable, SCHEDULE] + passthrough)

    # 固定从仓库根目录跑，保证配置路径和 outdir 不随调用位置飘
    if os.path.realpath(os.getcwd()) != os.path.realpath(HERE):
        log(f"  (切到仓库根目录: {HERE})")
        os.chdir(HERE)

    log(BAR)
    log("CF IP Pool  (IPv4 / TCP only / plain IP:Port)   Win/Linux/Termux 一键入口")
    log(BAR)

    push_ready, note = preflight_push(args.gh_config, assume_yes=args.yes)

    run_cfg, why = pick_run_config(args.config)
    log(f"运行配置: {run_cfg}    （{why}）")

    rc = build(run_cfg, passthrough)
    finish(push_ready, note, run_cfg)
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
