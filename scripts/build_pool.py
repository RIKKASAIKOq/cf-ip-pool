#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF IP Pool Builder
==================
从 Cloudflare 官方 IPv4 网段生成「纯 IP:端口」可用池，默认凑够 2 万条。

流程:
  1. 拉取官方 ips-v4
  2. 拆成 /24，每个 /24 随机取 N 个 IP，每个 IP 随机分配 1 个加密端口
  3. 并发 TCP 探测（连接超时 2 秒），连上即算可用
  4. 输出 ipv4.txt：只有 TCP 通的 IP:端口，凑够目标条数

不涉及 GeoIP、不读 colo、不产国家字段、不做 TLS 握手、不碰 IPv6。
输出就是纯净的 IP:端口。
"""

import argparse
import ipaddress
import json
import os
import random
import socket
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── 配置 ────────────────────────────────────────────────────────────────────

CF_IPV4_URL = "https://www.cloudflare.com/ips-v4"

CONNECT_TIMEOUT = 2.0        # TCP 连接超时（秒）
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Cloudflare 常用的 6 个 HTTPS(加密) 端口，每个 IP 随机分一个
ENCRYPTED_PORTS = [443, 8443, 2053, 2083, 2087, 2096]

DEFAULT_CONFIG = {
    "sample": 6,                     # 每个 /24 取几个 IP（起始值，不够会自动加）
    "ports": list(ENCRYPTED_PORTS),  # 加密端口池，每个 IP 随机取一个
    "workers": 1000,                 # 并发数
    "timeout": CONNECT_TIMEOUT,      # TCP 连接超时（秒）
    "target": 20000,                 # 目标存活条数（2w）
    "sample_step": 2,                # 每轮补采样时每个 /24 增加几个
    "max_rounds": 6,                 # 最多补采样几轮
    "outdir": "output",              # 输出目录
    "header": True,                  # ipv4.txt 首行是否写 # 注释头
    "seed": None,                    # 随机种子
}

CONFIG_TEMPLATE = """\
{
  "_comment": [
    "CF IP Pool Builder 配置文件 (JSON)",
    "用法: python scripts/build_pool.py --config config.json",
    "命令行参数优先级高于此文件（--sample 会覆盖 sample）",
    "留空或删除某行 = 用内置默认值"
  ],

  "_sample": "每个 /24 随机取几个 IP（起始值）。不够 target 时自动按 sample_step 加码重采",
  "sample": 6,

  "_ports": [
    "加密端口池。每个 IP 只随机分其中一个，所以 6 个端口会大致均匀混在结果里。",
    "443 / 8443 / 2053 / 2083 / 2087 / 2096 是 CF 常用的 HTTPS 端口"
  ],
  "ports": [443, 8443, 2053, 2083, 2087, 2096],

  "_workers": "并发线程数。1000 下纯 TCP 探测很稳；再高本地句柄/端口可能吃紧",
  "workers": 1000,

  "_timeout": "TCP 连接超时（秒）。2 秒是「宁可略过也不干等」的甜点位",
  "timeout": 2.0,

  "_target": "目标存活条数。默认 20000（2w）；网络差凑不够也不报错，给多少算多少",
  "target": 20000,

  "_sample_step": "每轮补采样时，每个 /24 的取样数增加多少",
  "sample_step": 2,

  "_max_rounds": "最多补采样几轮。轮数越多越可能凑够 target，但耗时也线性上涨",
  "max_rounds": 6,

  "_outdir": "输出目录（相对于当前工作目录）。只产出 ipv4.txt 和 meta.json",
  "outdir": "output",

  "_header": [
    "ipv4.txt 首行是否写 # 注释头（记录生成时间/行数）。",
    "下游解析器一般都会跳过 # 行；设 false 就是纯 IP:端口、一行不多"
  ],
  "header": true,

  "_seed": "随机种子，用于复现同样的采样/端口分配结果；null = 每次随机",
  "seed": null
}
"""


def _toml_parse(text):
    """
    极简 TOML 子集解析器，仅支持本脚本用到的标量与数组。
    不依赖 tomllib（Python 3.11+）或第三方库。
    """
    cfg = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().strip('"').strip("'")
        val = val.strip()

        # 去掉行内注释（仅当注释前是空白时）
        if "#" in val:
            head, _, tail = val.partition("#")
            # 避免把字符串里的 # 误当注释
            if not (head.count('"') % 2 or head.count("'") % 2):
                val = head.strip()

        if not val:
            continue

        # 布尔
        if val.lower() == "true":
            cfg[key] = True
            continue
        if val.lower() == "false":
            cfg[key] = False
            continue
        if val.lower() in ("null", "none"):
            cfg[key] = None
            continue

        # 数组  [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            body = val[1:-1].strip()
            items = []
            if body:
                for part in body.split(","):
                    p = part.strip().strip('"').strip("'")
                    if p:
                        # 元素本身能转 int 就转，便于 ports 这类数字数组直接用
                        try:
                            items.append(int(p))
                        except ValueError:
                            items.append(p)
            cfg[key] = items
            continue

        # 字符串
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            cfg[key] = val[1:-1]
            continue

        # 数字
        try:
            cfg[key] = int(val)
            continue
        except ValueError:
            pass
        try:
            cfg[key] = float(val)
            continue
        except ValueError:
            pass

        cfg[key] = val
    return cfg


def load_config(path):
    """读取配置文件（.toml 用内置解析器，.json 用 json 模块）。"""
    if not path or not os.path.exists(path):
        if path:
            log(f"      config not found: {path} (using defaults)")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith(".json"):
        cfg = json.loads(text.lstrip("\ufeff"))
    else:
        cfg = _toml_parse(text.lstrip("\ufeff"))
    # 去掉 JSON 模板里 _ 开头的说明键，再过滤未知键
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    unknown = [k for k in cfg if k not in DEFAULT_CONFIG]
    if unknown:
        log(f"      config ignored unknown keys: {', '.join(unknown)}")
    return {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}


def log(msg):
    print(msg, flush=True)


# ─── 1. 拉取官方网段 ─────────────────────────────────────────────────────────

def fetch_url(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_official_cidrs():
    """只拉 ips-v4。本工具只做 IPv4 纯 TCP 池，不碰 IPv6。"""
    log("[1/4] Fetching official CF IPv4 CIDRs ...")
    try:
        text = fetch_url(CF_IPV4_URL)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
    except Exception as e:
        log(f"      IPv4 fetch failed: {e}")
        lines = []
    log(f"      IPv4: {len(lines)} ranges")
    return lines


# ─── 2. 拆子网 + 采样 ────────────────────────────────────────────────────────

def host_ranges(cidrs):
    """
    把网段拆成 /24 粒度，返回可分配主机区间 (lo, hi) 闭区间列表（整数形式）。

    prefixlen < 24 的按 /24 切开（跳过 .0 网络地址和 .255 广播地址）；
    prefixlen >= 24 的（如 /25、/32）整段当一个区间，同样跳过首尾。
    """
    ranges = []
    for block in cidrs:
        try:
            net = ipaddress.IPv4Network(block, strict=False)
        except ValueError:
            continue
        base = int(net.network_address)
        size = 1 << (32 - net.prefixlen)
        if net.prefixlen < 24:
            blk = 1 << 8                       # /24 = 256
            n_blocks = size // blk
            for i in range(n_blocks):
                s = base + i * blk
                ranges.append((s + 1, s + blk - 2))
        elif size <= 2:                        # /31、/32 没有可跳过的首尾
            ranges.append((base, base + size - 1))
        else:
            ranges.append((base + 1, base + size - 2))
    return ranges


def total_hosts(ranges):
    return sum(hi - lo + 1 for lo, hi in ranges)


def sample_ips(ranges, k, exclude):
    """
    每个 /24 区间随机取 k 个 IP（整数），跳过 exclude 里已探测过的。
    返回打乱顺序的新 IP 列表。
    """
    out = []
    for lo, hi in ranges:
        n = hi - lo + 1
        kk = min(k, n)
        if kk <= 0:
            continue
        for off in random.sample(range(n), kk):
            ip = lo + off
            if ip in exclude:
                continue
            out.append(ip)
    random.shuffle(out)
    return out


# ─── 3. 并发 TCP 探测 ────────────────────────────────────────────────────────

_lock = threading.Lock()
_prog = {"done": 0, "alive": 0}
_errs = Counter()
_errs_lock = threading.Lock()


def _max_fds():
    """
    当前进程能同时打开的文件描述符上限；拿不到就返回 None。

    Windows 没有 ulimit 这套东西，也没有 resource 模块，所以直接返回 None，
    由调用方按「不限制」处理。
    """
    try:
        import resource  # noqa: F401  (仅 POSIX)
    except ImportError:
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return None
    # RLIM_INFINITY 在不同平台是 -1 或 2**64-1，都当成「没限制」
    if soft in (resource.RLIM_INFINITY, -1) or soft <= 0:
        return None
    return int(soft)


def cap_workers(workers, reserve=64):
    """
    把 workers 压到「不会把 fd 用光」的范围，返回 (调整后值, 提示或 None)。

    每个在飞的探测占 1 个 fd，加上 stdin/stdout/stderr，
    所以按 cap 的 80% 且留 reserve 个给系统，取更严的那个。
    限制很低（比如 256）时宁可降并发，也不要让一堆连接报
    Too many open files —— 那会被当成「IP 不通」，悄悄压低命中率。
    """
    cap = _max_fds()
    if cap is None:
        return workers, None
    limit = max(1, min(cap - reserve, int(cap * 0.8)))
    if workers <= limit:
        return workers, None
    return limit, (f"workers {workers} -> {limit} "
                   f"(fd limit {cap}, reserved {reserve} for the system)")


def _bump(done=0, alive=0):
    with _lock:
        _prog["done"] += done
        _prog["alive"] += alive


def _note_err(exc):
    with _errs_lock:
        _errs[type(exc).__name__] += 1


def probe_ip(ip, port, timeout):
    """
    纯 TCP 连接探测：连上即算可用。

    ip 可以是 int 或 str（内部统一用 int 做去重/排序，探测前转成点分十进制）。
    不做 TLS 握手、不读任何内容、不解析响应。timeout 秒连不上就当不可用。
    """
    host = ip if isinstance(ip, str) else str(ipaddress.IPv4Address(ip))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except Exception as e:
        _note_err(e)
        ok = False
    # done/alive 分开计数：alive 只统计真的连上的，进度日志才看得出命中率
    _bump(done=1, alive=1 if ok else 0)
    return (ip, port) if ok else None


def probe_pairs(pairs, workers, timeout, label=""):
    log(f"      probing {len(pairs)} (IP,port) pairs | workers {workers} | "
        f"timeout {timeout}s {label}")
    with _lock:
        _prog["done"] = 0
        _prog["alive"] = 0
    with _errs_lock:
        _errs.clear()

    out = []
    last = time.time()
    total = len(pairs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe_ip, ip, port, timeout) for ip, port in pairs]
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                out.append(r)
            now = time.time()
            if now - last >= 5:
                with _lock:
                    d, a = _prog["done"], _prog["alive"]
                log(f"        progress {d}/{total} ({d*100/max(1,total):.1f}%)  alive {a}")
                last = now

    # 失败原因分布：全是 0 存活时，靠这个一眼看出是系统性错误而不是真不通
    with _errs_lock:
        top = _errs.most_common(5)
    if top:
        log("        fail reasons: "
            + ", ".join(f"{k} {v}" for k, v in top))
    if not out and pairs:
        log("        ! 0 alive -- check network/DNS/firewall, not just CF reachability")

    return out


def build_pool(ranges, cfg):
    """
    采样 + 探测，直到凑够 target 条存活记录。

    每个 IP 只探测一次；端口从 cfg["ports"] 里随机取一个。
    一轮不够就按 sample_step 加大每 /24 取样数再采新 IP，最多 max_rounds 轮。
    """
    target = int(cfg["target"])
    ports = [int(p) for p in cfg["ports"]]
    if not ports:
        log("no ports configured, exit")
        sys.exit(1)
    workers = int(cfg["workers"])
    timeout = float(cfg["timeout"])
    step = max(1, int(cfg.get("sample_step", 2)))
    max_rounds = max(1, int(cfg.get("max_rounds", 6)))
    # 预估命中率，用来决定第一轮采多少（宁多勿少，减少往返轮次）；
    # 第二轮起改用上一轮的实测值
    hit_rate = 0.9

    capacity = total_hosts(ranges)
    log(f"[2/4] Sampling from {len(ranges)} /24 blocks "
        f"({capacity} usable host addresses)")

    tried = set()
    alive = []
    seen_pairs = set()
    k = max(1, int(cfg["sample"]))

    for rnd in range(1, max_rounds + 1):
        need = target - len(alive)
        if need <= 0:
            break

        want = int(need / hit_rate) + 500
        batch = sample_ips(ranges, k, tried)[:want]
        if not batch:
            log(f"      round {rnd}: no unsampled IPs left "
                f"(tried {len(tried)}/{capacity})")
            break
        tried.update(batch)

        log(f"[3/4] round {rnd}/{max_rounds}: k={k} per /24, "
            f"{len(batch)} new IPs (covered {len(tried)}/{capacity})")
        pairs = [(ip, random.choice(ports)) for ip in batch]
        got = probe_pairs(pairs, workers, timeout, f"(round {rnd})")
        for ip, port in got:
            if (ip, port) in seen_pairs:
                continue
            seen_pairs.add((ip, port))
            alive.append((ip, port))

        log(f"      round {rnd}: alive {len(got)} / {len(batch)} "
            f"({len(got)*100/max(1,len(batch)):.1f}%)  total {len(alive)}/{target}")
        if len(alive) >= target:
            break
        k += step
        # 用实测命中率替换预估，下一轮采得更准
        hit_rate = max(0.05, min(1.0, len(got) / max(1, len(batch))))

    # 实际探测的 IP 数（不含探测完就被丢掉的存活记录），用于总结里的准确统计
    stats = {
        "probed": len(tried),
        "alive": len(alive),
        "kept": min(len(alive), target),
    }

    if len(alive) < target:
        log(f"      ! only reached {len(alive)}/{target} after "
            f"{max_rounds} rounds; using what we got")

    return alive[:target], stats


# ─── 4. 输出 ─────────────────────────────────────────────────────────────────

def _now_cn():
    """当前北京时间（UTC+8）。CI 与本地都用它，保证时间戳一致。
    不依赖 tzdata，直接拿 UTC 加 8 小时，避免 Windows/精简容器没有时区库。"""
    return time.gmtime(time.time() + 8 * 3600)


def _stamp_cn():
    """北京时间字符串，例如 2026-10-16 14:30:05"""
    return time.strftime("%Y-%m-%d %H:%M:%S", _now_cn())


def _sort_key(r):
    """IPv4 按整数地址排序（不是字符串排序）。"""
    try:
        return int(ipaddress.ip_address(r["ip"]))
    except ValueError:
        return 0


def _write_list(path, rows, stamp, kind="", header=True):
    """写 IP:端口 列表文件。

    默认首行是注释头（以 # 开头），记录生成时间/行数。
    下游解析器都会跳过 # 开头的行，所以不影响使用，
    但打开文件就能看到「这批数据是什么时候出的」。
    header=False 时一行不多，纯 IP:端口。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if stamp and header:
            tag = f" {kind}" if kind else ""
            f.write(f"# Cloudflare IP Pool{tag} | {stamp} (UTC+8) | "
                    f"{len(rows)} IPs | IP:Port\n")
        for r in rows:
            f.write(f"{r['ip']}:{r['port']}\n")


def write_outputs(pairs, outdir, cfg):
    """
    只输出 ipv4.txt（纯 IP:端口，仅 TCP 通的）。

    不查 GeoIP、不读 colo、不产国家字段、不输出 IPv6 / 混合表 / CSV。
    meta.json 只是个小的元信息边车（时间、条数、端口分布），不是 IP 池文件。
    """
    log("[4/4] Writing outputs ...")
    os.makedirs(outdir, exist_ok=True)

    seen = set()
    rows = []
    for ip, port in pairs:
        key = (ip, port)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "ip": str(ipaddress.IPv4Address(ip)),
            "port": int(port),
            "ver": 4,
        })

    rows.sort(key=_sort_key)

    # 统一时间戳：文件头、meta.json、提交信息都用它（北京时间 UTC+8）
    stamp = _stamp_cn()

    v4_txt = os.path.join(outdir, "ipv4.txt")
    meta_json = os.path.join(outdir, "meta.json")

    # 唯一输出: IP:端口（仅 IPv4，仅 TCP 通）
    _write_list(v4_txt, rows, stamp, "IPv4", header=bool(cfg.get("header", True)))

    # 端口分布
    port_cnt = Counter(r["port"] for r in rows)

    with open(meta_json, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": stamp + " UTC+8",
            "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "total": len(rows),
            "unique_ips": len({r["ip"] for r in rows}),
            "ports": {str(k): v for k, v in port_cnt.most_common()},
            "probe": {
                "mode": "tcp",
                "workers": int(cfg["workers"]),
                "timeout": float(cfg["timeout"]),
                "target": int(cfg["target"]),
            },
        }, f, ensure_ascii=False, indent=2)

    log(f"      stamp: {stamp} UTC+8")
    log(f"      {v4_txt}    ({len(rows)} lines)")
    log(f"      {meta_json}")
    return rows


# ─── 主流程 ──────────────────────────────────────────────────────────────────

def _cli_overrides(args):
    """
    只收集「用户真的在命令行显式给了」的参数。
    办法是扫 sys.argv，避免用 None 做默认值时无法区分「没给」和「给了空」。
    """
    given = set()
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            given.add(name)
        elif i > 0 and argv[i - 1].startswith("--"):
            # 空格分隔的取值形式：--sample 3
            given.add(argv[i - 1][2:].split("=", 1)[0])
    return given


def main():
    """
    主流程：CLI 参数解析 → 配置合并 → 拉网段 → 采样探测 → 输出。
    """
    ap = argparse.ArgumentParser(
        description="Build Cloudflare IPv4 IP pool (plain IP:port, TCP only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="可用 --init-config 生成默认 config.json 模板")
    ap.add_argument("--config", type=str, default="config.json",
                    help="配置文件路径 (默认 config.json，不存在则用内置默认)")
    ap.add_argument("--init-config", action="store_true",
                    help="生成默认 config.json 模板后退出")
    ap.add_argument("--sample", type=int, default=None,
                    help="每个 /24 取几个 IP (默认 6，不够会自动加码)")
    ap.add_argument("--ports", type=str, default=None,
                    help="加密端口，逗号分隔 (默认 443,8443,2053,2083,2087,2096)")
    ap.add_argument("--workers", type=int, default=None, help="并发数 (默认 1000)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="TCP 连接超时秒数 (默认 2.0)")
    ap.add_argument("--target", type=int, default=None,
                    help="目标存活条数 (默认 20000)")
    ap.add_argument("--outdir", type=str, default=None, help="输出目录")
    ap.add_argument("--no-header", action="store_true",
                    help="ipv4.txt 首行不写 # 注释头，输出纯 IP:端口")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    args = ap.parse_args()

    # --init-config 特殊模式：生成模板后立即退出
    if args.init_config:
        target = args.config or "config.json"
        if os.path.exists(target):
            log(f"{target} already exists, not overwritten")
        else:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(CONFIG_TEMPLATE)
            log(f"wrote {target}")
        return

    # 配置优先级：内置默认 < config.json < 命令行显式参数
    # 用 _cli_overrides 扫 argv 判断「用户到底给了哪些」，避免 None 歧义
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        cfg.update(load_config(args.config))
    given = _cli_overrides(args)

    if "sample" in given:
        cfg["sample"] = args.sample
    if "ports" in given:
        cfg["ports"] = [p.strip() for p in (args.ports or "").split(",") if p.strip()]
    if "workers" in given:
        cfg["workers"] = args.workers
    if "timeout" in given:
        cfg["timeout"] = args.timeout
    if "target" in given:
        cfg["target"] = args.target
    if "outdir" in given:
        cfg["outdir"] = args.outdir
    if "no_header" in given:
        cfg["header"] = not args.no_header
    if "seed" in given:
        cfg["seed"] = args.seed

    if cfg["seed"] is not None:
        random.seed(cfg["seed"])

    # fd 上限保护：并发探得动才叫并发，超了会被 OS 拒连，
    # 报出来还长得很像「IP 不通」，所以这里提前压一压
    capped, note = cap_workers(int(cfg["workers"]))
    if note:
        cfg["workers"] = capped
        log(f"  ! {note}")

    t0 = time.time()
    log("=" * 60)
    log("CF IP Pool Builder  (IPv4 / TCP only / plain IP:Port)")
    if args.config and os.path.exists(args.config):
        log(f"config: {args.config}")
    log("=" * 60)
    log(f"sample={cfg['sample']}(+{cfg['sample_step']}/round, max {cfg['max_rounds']} rounds) "
        f"target={cfg['target']}")
    log(f"ports={cfg['ports']} workers={cfg['workers']} "
        f"timeout={cfg['timeout']}s outdir={cfg['outdir']}"
        + ("" if cfg.get("header", True) else " (no header)"))

    # 1. 拉取官方 CIDR（只 ips-v4）
    cidrs = fetch_official_cidrs()
    if not cidrs:
        log("no CIDRs fetched, exit")
        sys.exit(1)

    # 2. 拆 /24 + 自适应采样 + 3. 并发 TCP 探测
    ranges = host_ranges(cidrs)
    if not ranges:
        log("no usable host ranges, exit")
        sys.exit(1)
    pairs, stats = build_pool(ranges, cfg)

    # 4. 输出（只有 ipv4.txt）
    rows = write_outputs(pairs, cfg["outdir"], cfg)

    log("=" * 60)
    log(f"finished in {time.time()-t0:.1f}s")
    log(f"  probed {stats['probed']} IPs -> alive {stats['alive']} "
        f"-> kept {stats['kept']} (target {cfg['target']})")
    if rows:
        port_cnt = Counter(r["port"] for r in rows)
        log(f"  ports: {', '.join(f'{k}:{v}' for k,v in port_cnt.most_common())}")
    log("=" * 60)


if __name__ == "__main__":
    main()
