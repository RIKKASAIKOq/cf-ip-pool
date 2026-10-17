#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Push Preflight / 推送链路自检
===========================

单独验证「在自己机器上跑完能不能推成功」，不用真跑一遍 build_pool，也不改动仓库内容。

检查项（按顺序，前面挂了后面就不跑）：
  1. 配置文件    gh_config.json 在不在、JSON 合不合法、推送键填了没、是不是 your_ 占位符
  2. token       从环境变量 GITHUB_TOKEN / GH_TOKEN / 配置里取，只打码不打印
  3. git 环境    是不是 git 仓、当前分支、origin 在哪、目标 owner/repo 和 branch 怎么解析
  4. 网络        DNS 解析 github.com + TCP 443 连通性
  5. 认证与权限  git ls-remote（读）+ git push --dry-run（写权限 / 分支保护）
  6. 真推送      --real-push 时推 HEAD:refs/heads/<branch>（已是最新则空跑，安全）

用法::

    python scripts/check_push.py                      # 默认探测模式，不改任何东西
    python scripts/check_push.py --real-push          # 真推一次（up-to-date 时是空跑）
    python scripts/check_push.py --config other.json  # 换一份推送配置
    python scripts/check_push.py --no-network         # 只查配置和 git 环境，不联网

退出码：0 = 全部关键项通过；1 = 有关键项失败。
日志里绝不出现完整 token，只显示长度和前几位特征。
"""

import argparse
import json
import os
import socket
import subprocess
import sys

# 复用 build_pool 里已经写好的推送逻辑，避免两套实现漂移
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_pool as bp  # noqa: E402

GH_HOST = "github.com"
GH_PORT = 443

# 检查结果计数
STATS = {"ok": 0, "fail": 0, "warn": 0, "skip": 0}


def _ok(msg):
    STATS["ok"] += 1
    print(f"[ OK ] {msg}", flush=True)


def _fail(msg, hint=""):
    STATS["fail"] += 1
    print(f"[FAIL] {msg}", flush=True)
    if hint:
        print(f"       {hint}", flush=True)


def _warn(msg, hint=""):
    STATS["warn"] += 1
    print(f"[WARN] {msg}", flush=True)
    if hint:
        print(f"       {hint}", flush=True)


def _skip(msg):
    STATS["skip"] += 1
    print(f"[SKIP] {msg}", flush=True)


def _info(msg):
    print(f"       {msg}", flush=True)


def _section(title):
    print("", flush=True)
    print("── " + title + " " + "─" * max(0, 56 - len(title)), flush=True)


def _fingerprint(token):
    """token 的「指纹」：只暴露长度和前后几位，够辨认是哪一个，又不足以被盗用。"""
    t = (token or "").strip()
    if not t:
        return ""
    if len(t) <= 8:
        return f"<{len(t)} chars>"
    return f"{t[:4]}…{t[-2:]}  ({len(t)} chars)"

# ─── 1. 配置文件 ─────────────────────────────────────────────────────────────

def load_push_cfg(path, cfg_path):
    """
    定位并读取推送配置，返回 (cfg, path, problems)。

    cfg 只含推送相关键（token / username / repo_name / branch / auto_push）。
    找不到文件、读不出来、顶层不是对象，都算致命问题。
    """
    problems = []
    cands = bp._gh_config_candidates(path, cfg_path)
    found = None
    for p in cands:
        if os.path.exists(p):
            found = p
            break
    if not found:
        return None, None, [f"找不到推送配置（找过: {', '.join(cands)}）"]

    try:
        with open(found, "r", encoding="utf-8") as f:
            raw = json.loads(f.read().lstrip("\ufeff"))
    except Exception as e:
        return None, found, [f"{found} 读取/解析失败: {e}"]
    if not isinstance(raw, dict):
        return None, found, [f"{found} 顶层不是 JSON 对象"]

    cfg = {k: v for k, v in raw.items() if k in bp.PUSH_KEYS}
    missing = [k for k in ("github_token", "github_username", "repo_name", "branch")
               if not cfg.get(k)]
    if missing:
        problems.append(f"{found} 里这些推送键是空的: {', '.join(missing)}")
    return cfg, found, problems


# ─── 2~5. 逐项检查 ──────────────────────────────────────────────────────────

def check_git_env(cfg):
    """git 环境：是不是仓、当前分支、origin、目标 repo/branch 怎么解析。"""
    _section("git 环境")

    if bp._git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        _fail("当前目录不在 git 工作树里", "cd 到仓库根目录再跑（比如 F:\\deepseek\\cf-ip-pool）")
        return False
    _ok("在 git 工作树里")

    head = bp._git(["rev-parse", "--verify", "HEAD"])
    if head.returncode != 0:
        _fail("本地还没有任何提交", "先 git commit 一次，否则没有东西可推")
        return False
    _ok(f"HEAD = {head.stdout.strip()[:12]}")

    cur = (bp._git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout or "").strip()
    _info(f"当前分支: {cur or '(detached)'}")

    url = (bp._git(["remote", "get-url", "origin"]).stdout or "").strip()
    if url:
        _ok(f"origin = {bp._mask(url)}")
    else:
        _warn("没有 origin remote", "github_username + repo_name 也没填时就只能靠 token + 手写仓库名")

    branch = bp._resolve_branch(cfg)
    if not branch:
        _fail("解析不出目标分支", "在配置里写 branch = \"main\"，或先切到一个具名分支")
        return False
    _ok(f"目标分支: {branch}")

    repo = bp._resolve_repo(cfg)
    if not repo:
        _fail("解析不出目标仓库 owner/repo",
              "在 gh_config.json 里写 github_username + repo_name（或 github_repo = \"owner/repo\"）")
        return False
    _ok(f"目标仓库: {repo}")
    return True


def check_network(timeout):
    """DNS + TCP 443 连通性。"""
    _section("网络连通性")
    try:
        infos = socket.getaddrinfo(GH_HOST, GH_PORT, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        _ok(f"DNS {GH_HOST} -> {', '.join(ips[:3])}" + (" …" if len(ips) > 3 else ""))
    except Exception as e:
        _fail(f"DNS 解析 {GH_HOST} 失败: {e}", "检查网络 / DNS，或挂代理")
        return False

    try:
        with socket.create_connection((GH_HOST, GH_PORT), timeout=timeout):
            pass
        _ok(f"TCP {GH_HOST}:{GH_PORT} 可连接")
    except Exception as e:
        _fail(f"TCP {GH_HOST}:{GH_PORT} 连不上: {e}", "检查防火墙 / 代理；公司网 often 拦 443")
        return False
    return True


def _push_url(cfg, repo):
    """有 token 就拼临时 URL，否则退回 origin。返回 (git 用的 remote 参数, 说明)。"""
    token = bp._resolve_token(cfg)
    if token and repo:
        return (f"https://x-access-token:{token}@github.com/{repo}.git",
                f"{repo} (token)")
    return ("origin", "origin (本机 git 凭据)")


def check_auth(cfg, repo, branch, timeout, real_push=False):
    """认证与权限：ls-remote 读、push --dry-run 写、（可选）真推。"""
    _section("认证与权限" + ("" if not real_push else " + 真推送"))

    remote, label = _push_url(cfg, repo)
    token = bp._resolve_token(cfg)
    if not token:
        _warn("没有 token，将使用本机 git 凭据",
              "配了 token 才能脱离本机凭据；环境变量 GITHUB_TOKEN 优先级最高")
    else:
        src = "环境变量 GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else (
            "环境变量 GH_TOKEN" if os.environ.get("GH_TOKEN") else "配置里的 github_token")
        _ok(f"token 来源: {src}  指纹: {_fingerprint(token)}")
    _info(f"推送目标: {label} @ {branch}")

    # 读权限
    r = bp._git(["ls-remote", remote, "HEAD"])
    if r.returncode != 0:
        raw = (r.stderr or r.stdout or "").strip()
        _fail(f"ls-remote 失败（读不到远端）: {bp._mask(raw)}", _hint_for(raw))
        return False
    sha = (r.stdout or "").strip().split()
    _ok(f"能读到远端 HEAD" + (f" = {sha[0][:12]}" if sha else ""))

    # 写权限 / 分支保护（dry-run 不做真实更新）
    if not real_push:
        r = bp._git(["push", "--dry-run", remote, f"HEAD:refs/heads/{branch}"])
        if r.returncode != 0:
            raw = (r.stderr or r.stdout or "").strip()
            _fail(f"push --dry-run 被拒（写不进去）: {bp._mask(raw)}", _hint_for(raw))
            return False
        out = (r.stderr or r.stdout or "").strip()
        _ok("push --dry-run 通过（有写权限，分支也没被保护挡下）")
        if out:
            _info(bp._mask(out).splitlines()[0][:100])
    else:
        r = bp._git(["push", remote, f"HEAD:refs/heads/{branch}"])
        if r.returncode != 0:
            raw = (r.stderr or r.stdout or "").strip()
            _fail(f"真推送失败: {bp._mask(raw)}", _hint_for(raw))
            return False
        out = (r.stderr or r.stdout or "").strip()
        _ok(f"真推送成功: {label} @ {branch}")
        if out:
            _info(bp._mask(out).splitlines()[0][:100])
    return True


def _hint_for(raw):
    """按 git 报错内容给排查建议。"""
    low = (raw or "").lower()
    if "authentication failed" in low or "401" in low or "invalid username or token" in low \
            or "invalid username or password" in low:
        return "token 无效/过期；fine-grained PAT 需要该仓库 Contents: Read and write"
    if "403" in low or "denied" in low or "permission" in low:
        return "token 权限不足（要 Contents 写权限），或仓库不在 token 授权范围"
    if "not found" in low or "404" in low:
        return "仓库名不对（github_username / repo_name），或 token 无权访问该仓库"
    if "protected" in low or "gh006" in low:
        return "目标分支受保护；换一个分支，或给 token 对应的账号开写权限"
    if "non-fast-forward" in low or "rejected" in low or "fetch first" in low:
        return "远端有新提交；先 git pull --rebase 再推"
    if "could not resolve host" in low or "timed out" in low or "connection" in low:
        return "网络问题；检查代理 / 防火墙"
    return "检查 token、仓库名、分支名与网络"


# ─── main ──────────────────────────────────────────────────────────────────

def _summary():
    print("", flush=True)
    print("=" * 60, flush=True)
    s = STATS
    line = (f"OK {s['ok']}   FAIL {s['fail']}   WARN {s['warn']}   SKIP {s['skip']}")
    print(line, flush=True)
    if s["fail"]:
        print("结论: 推送链路有问题，按上面的 hint 逐条排查", flush=True)
    elif s["warn"]:
        print("结论: 能推，但有警告项（比如没有 token，靠本机凭据）", flush=True)
    else:
        print("结论: 推送链路通畅，可以放心用 --push / auto_push", flush=True)
    print("=" * 60, flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="推送链路自检：确认在自己机器上能不能成功推送到 GitHub（默认不改任何东西）",
        epilog="默认只做探测（ls-remote + push --dry-run）；加 --real-push 才真推")
    ap.add_argument("--config", type=str, default=None,
                    help="推送配置路径 (默认自动找 gh_config.json)")
    ap.add_argument("--cfg", type=str, default=None,
                    help="运行配置路径，用于在同目录里找 gh_config.json")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="网络检查超时秒数 (默认 10)")
    ap.add_argument("--no-network", action="store_true",
                    help="跳过 DNS/TCP 检查（离线环境只想看配置和 git 解析）")
    ap.add_argument("--real-push", action="store_true",
                    help="真推一次 HEAD:refs/heads/<branch>；已是最新则是空跑，不会覆盖远端")
    args = ap.parse_args()

    print("=" * 60, flush=True)
    print("Push Preflight  (只验证推送链路，不跑 build_pool)", flush=True)
    print("=" * 60, flush=True)

    # 1. 配置文件
    _section("配置文件")
    cfg, found, problems = load_push_cfg(args.config, args.cfg)
    if cfg is None:
        for p in problems:
            _fail(p)
        _info("生成模板: python scripts/build_pool.py --init-gh-config")
        _summary()
        sys.exit(1)
    _ok(f"读到 {found}")
    _info(f"推送键: {', '.join(sorted(cfg)) or '(空)'}")
    for p in problems:
        _warn(p, "留空不一定是错：token 可以走环境变量，仓库名可以从 origin 解析")

    ok, ph = bp._check_push_config(cfg)
    if not ok:
        for p in ph:
            _fail(p)
        _info("your_ 开头的值会被当占位符拦下，换成真实值再试")
        _info("token 去 https://github.com/settings/tokens 生成（classic 勾 repo，fine-grained 给本仓库 Contents: Read and write）")
        _info("github_username / repo_name 照抄仓库地址：github.com/RikkaSaiko1/cf-ip-pool → RikkaSaiko1 + cf-ip-pool")
        _summary()
        sys.exit(1)
    _ok("没有 your_ 占位符")

    # 2. token
    _section("token")
    token = bp._resolve_token(cfg)
    if token:
        src = ("环境变量 GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN")
               else "环境变量 GH_TOKEN" if os.environ.get("GH_TOKEN")
               else f"{found} 里的 github_token")
        _ok(f"取到 token：{_fingerprint(token)}  (来源: {src})")
    else:
        _warn("没拿到 token，将退回本机 git 凭据",
              "要脱离本机凭据就设环境变量 GITHUB_TOKEN，或写进 gh_config.json")

    # 3. git 环境（失败就不必往下测了）
    if not check_git_env(cfg):
        _summary()
        sys.exit(1)

    repo = bp._resolve_repo(cfg)
    branch = bp._resolve_branch(cfg)

    # 4. 网络
    if args.no_network:
        _skip("网络检查（--no-network）")
    elif not check_network(args.timeout):
        _summary()
        sys.exit(1)

    # 5. 认证 / 权限 / （可选）真推送
    if not check_auth(cfg, repo, branch, args.timeout, args.real_push):
        _summary()
        sys.exit(1)

    sys.exit(0 if STATS["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
