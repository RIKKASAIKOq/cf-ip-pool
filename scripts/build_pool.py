#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF IP Pool Builder
==================
从 Cloudflare 官方 IP 段生成 IP 池，附带国家(GeoIP)与 CF 机房(colo)信息。

流程:
  1. 拉取官方 ips-v4 / ips-v6
  2. 拆成 /24 (v4) 与 /48 (v6)，每个子网随机取 N 个 IP
  3. 查 GeoIP 国家（maxminddb 库，或降级到 ip2country 文本库）
  4. 并发 TCP 探测，连上的读 /cdn-cgi/trace 拿 colo(CF机房)
  5. 输出 all.txt (IP:端口#国家) 与 all_full.csv

依赖: maxminddb (可选，缺失时国家字段为空)
"""

import argparse
import csv
import ipaddress
import json
import os
import random
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── 配置 ────────────────────────────────────────────────────────────────────

CF_IPV4_URL = "https://www.cloudflare.com/ips-v4"
CF_IPV6_URL = "https://www.cloudflare.com/ips-v6"

TRACE_HOST = "speed.cloudflare.com"
TRACE_PATH = "/cdn-cgi/trace"

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


# ─── CF 机房代码 -> 国家 ─────────────────────────────────────────────────────
# colo 是 Cloudflare 的数据中心 IATA 代码（如 SIN=新加坡）。
# 它反映「实际服务你的机房」，比 IP 注册地(GeoIP)有意义得多。
# 参考: https://www.cloudflarestatus.com/ （数据中心列表）
COLO_TO_COUNTRY = {
    # 亚太
    "SIN": "SG", "HKG": "HK", "NRT": "JP", "KIX": "JP", "ITM": "JP",
    "ICN": "KR", "TPE": "TW", "MFM": "MO",
    "BKK": "TH", "SGN": "VN", "HAN": "VN", "KUL": "MY", "CGK": "ID",
    "MNL": "PH", "BOM": "IN", "DEL": "IN", "MAA": "IN", "BLR": "IN",
    "CCU": "IN", "HYD": "IN", "AMD": "IN", "PNQ": "IN",
    "SYD": "AU", "MEL": "AU", "BNE": "AU", "PER": "AU", "ADL": "AU",
    "AKL": "NZ", "CHC": "NZ",
    "DAC": "BD", "CMB": "LK", "KTM": "NP", "ISB": "PK", "KHI": "PK", "LHE": "PK",
    "TAS": "UZ", "ALA": "KZ", "GUW": "KZ", "ASB": "TM", "EVN": "AM",
    "TBS": "GE", "GYD": "AZ", "BAK": "AZ", "OVB": "RU", "KJA": "RU",
    # 北美
    "LAX": "US", "SJC": "US", "SEA": "US", "PDX": "US", "SFO": "US",
    "IAD": "US", "EWR": "US", "ORD": "US", "DFW": "US", "ATL": "US",
    "MIA": "US", "DEN": "US", "PHX": "US", "MSP": "US", "DTW": "US",
    "BOS": "US", "PHL": "US", "CLT": "US", "IAH": "US", "SAN": "US",
    "YYZ": "CA", "YVR": "CA", "YUL": "CA", "YYC": "CA",
    "MEX": "MX", "GDL": "MX", "QRO": "MX",
    # 南美
    "GRU": "BR", "GIG": "BR", "CGH": "BR", "FOR": "BR", "POA": "BR",
    "EZE": "AR", "SCL": "CL", "BOG": "CO", "LIM": "PE", "UIO": "EC",
    "MVD": "UY", "ASU": "PY", "VVI": "BO", "CCS": "VE",
    # 欧洲
    "AMS": "NL", "FRA": "DE", "MUC": "DE", "DUS": "DE", "BER": "DE", "HAM": "DE",
    "LHR": "GB", "LGW": "GB", "MAN": "GB", "EDI": "GB", "GLA": "GB",
    "CDG": "FR", "MRS": "FR", "LYS": "FR", "TLS": "FR",
    "MAD": "ES", "BCN": "ES", "MXP": "IT", "FCO": "IT", "LIN": "IT",
    "WAW": "PL", "KRK": "PL", "ARN": "SE", "GOT": "SE", "HEL": "FI",
    "OSL": "NO", "CPH": "DK", "VIE": "AT", "BRU": "BE", "ZRH": "CH",
    "GVA": "CH", "DUB": "IE", "LIS": "PT", "OPO": "PT", "PRG": "CZ",
    "BUD": "HU", "OTP": "RO", "SOF": "BG", "ATH": "GR", "SKG": "GR",
    "ZAG": "HR", "BEG": "RS", "LJU": "SI", "BTS": "SK", "TLL": "EE",
    "RIX": "LV", "VNO": "LT", "KBP": "UA", "MSQ": "BY", "IST": "TR",
    "ADB": "TR", "TLV": "IL", "DME": "RU", "LED": "RU", "SVO": "RU",
    "KEF": "IS", "LUX": "LU", "MLA": "MT", "TIA": "AL", "SKP": "MK",
    # 中东 / 非洲
    "DXB": "AE", "AUH": "AE", "DOH": "QA", "KWI": "KW", "BAH": "BH",
    "MCT": "OM", "RUH": "SA", "JED": "SA", "AMM": "JO", "BEY": "LB",
    "BGW": "IQ", "IKA": "IR", "THR": "IR", "MHD": "IR",
    "JNB": "ZA", "CPT": "ZA", "DUR": "ZA", "CAI": "EG", "NBO": "KE",
    "LOS": "NG", "ABV": "NG", "ACC": "GH", "CMN": "MA", "TUN": "TN",
    "ALG": "DZ", "ADD": "ET", "DAR": "TZ", "MRU": "MU", "TNR": "MG",
    "DKR": "SN", "ABJ": "CI", "KRT": "SD", "LAD": "AO", "MPM": "MZ",
    "HRE": "ZW", "LUN": "ZM", "GBE": "BW", "WDH": "NA", "BLZ": "MW",
}


# ─── 配置文件支持 ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "sample": 3,                 # 每个 /24 取几个 IP（IPv4 采样粒度）
    "sample_v6": 1,              # 每个 /48 取几个 IP（IPv6 采样粒度）
    "v6_limit": 7650,            # IPv6 总上限（跨所有 CF 网段，7:3 比例用）
    "ports": [443, 8443],        # 端口列表（多端口会级联探测：第一个通才测下一个）
    "workers": 200,              # 并发数（过高会导致 TLS 握手失败、读不到 colo）
    "outdir": "output",          # 输出目录
    "mmdb": "GeoLite2-Country.mmdb",
    "no_v6": False,              # 跳过 IPv6
    "no_trace": False,           # 只测连通性，不读 colo
    "country_mode": "colo",      # colo=CF机房(推荐) / geoip=IP注册地
    "keep_unknown": False,       # 保留无法确定国家的 IP
    "seed": None,                # 随机种子
}

CONFIG_TEMPLATE = """\
{
  "_comment": [
    "CF IP Pool Builder 配置文件 (JSON)",
    "用法: python scripts/build_pool.py --config config.json",
    "命令行参数优先级高于此文件（--sample 会覆盖 sample）",
    "留空或删除某行 = 用内置默认值"
  ],

  "_sample": "每个子网取几个 IP：IPv4 固定到 /24 粒度(sample)，IPv6 固定到 /48 粒度(sample_v6)。3 = 每块随机抽 3 个（推荐）",
  "sample": 3,

  "_sample_v6": "IPv6 每个 /48 取几个 IP（比 IPv4 少，因 CF v6 网段极大）",
  "sample_v6": 1,

  "_v6_limit": "IPv6 总量上限（跨所有 CF 网段）。v4 按 sample 算约 17868，7:3 对应 v6 约 7650",
  "v6_limit": 7650,

  "_ports": [
    "探测端口列表。多端口是级联的：先测第一个，只对存活的 IP 补测后面的，",
    "所以第一个端口建议放主力。443/8443 都能读 colo(走 TLS)；80 是明文读不到 colo、会被丢弃"
  ],
  "ports": [443, 8443],

  "_workers": "并发线程数。太高会打爆本地 TLS/网络资源导致 colo 读不出（实测 800 掉到 27%）。200~300 安全区间",
  "workers": 200,

  "_outdir": "输出目录（相对于当前工作目录）",
  "outdir": "output",

  "_mmdb": "GeoIP 数据库路径。文件不存在时 country_geoip 列留空，但 colo 仍正常工作",
  "mmdb": "GeoLite2-Country.mmdb",

  "_no_v6": "跳过 IPv6 探测。本机无 IPv6 出网时设 true（省约 40% 时间）；GitHub Actions runner 有 IPv6，CI 上保持 false",
  "no_v6": false,

  "_no_trace": "只测 TCP 连通性，跳过 TLS + colo 读取（更快，但没有国家/机房信息）",
  "no_trace": false,

  "_country_mode": "国家判定来源: colo=按 CF 实际机房推算(推荐,Anycast 下最贴近真实延迟) / geoip=按 IP 注册地",
  "country_mode": "colo",

  "_keep_unknown": "保留无法确定国家的 IP（默认 false = 丢弃，数据更干净）",
  "keep_unknown": false,

  "_seed": "随机种子，用于复现同样的采样结果；null = 每次随机",
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


def colo_country(colo):
    """把 CF 机房代码翻译成国家二字码。未知返回原代码。"""
    if not colo:
        return ""
    return COLO_TO_COUNTRY.get(colo.upper(), "")


def log(msg):
    print(msg, flush=True)


# ─── 1. 拉取官方网段 ─────────────────────────────────────────────────────────

def fetch_url(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_official_cidrs():
    log("[1/5] Fetching official CF CIDRs ...")
    cidrs = {}
    for ver, url in ((4, CF_IPV4_URL), (6, CF_IPV6_URL)):
        try:
            text = fetch_url(url)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            cidrs[ver] = lines
            log(f"      IPv{ver}: {len(lines)} ranges")
        except Exception as e:
            log(f"      IPv{ver} fetch failed: {e}")
            cidrs[ver] = []
    return cidrs


# ─── 2. 拆子网 + 采样 ────────────────────────────────────────────────────────

def expand_and_sample(cidrs, sample_per_block, sample_v6=1, v6_limit=5000):
    """
    拆网段并采样。
    v4 按 sample_per_block（每 /24 取几个），v6 按 sample_v6（每 /48 取几个），
    v6 总量受 v6_limit 限制，用于实现 v4 多 v6 少的比例控制。
    """
    log("[2/5] Splitting subnets and sampling ...")
    ipv4, ipv6 = [], []
    v6_nets = []

    for block in cidrs.get(4, []):
        try:
            net = ipaddress.IPv4Network(block, strict=False)
        except ValueError:
            continue
        ipv4.extend(_sample_v4(net, sample_per_block))

    for block in cidrs.get(6, []):
        try:
            net = ipaddress.IPv6Network(block, strict=False)
        except ValueError:
            continue
        v6_nets.append(net)
    ipv6.extend(_sample_v6_global(v6_nets, sample_v6, v6_limit))

    random.shuffle(ipv4)
    random.shuffle(ipv6)
    log(f"      IPv4 sampled {len(ipv4)}, IPv6 sampled {len(ipv6)}")
    return ipv4, ipv6


def _sample_v4(net, k):
    """
    纯整数运算，避免构造海量对象。
    v4 固定到 /24 粒度，每块随机取 k 个（跳过网络地址/广播地址，即 .1~.254）。
    """
    out = []
    block_bits = 24 if net.prefixlen < 24 else net.prefixlen
    base = int(net.network_address)
    blk_size = 1 << (32 - block_bits)
    count_blocks = (1 << (32 - net.prefixlen)) // blk_size

    for i in range(count_blocks):
        blk_start = base + i * blk_size
        lo = blk_start + 1
        hi = blk_start + blk_size - 2
        if hi < lo:
            continue
        n_avail = hi - lo + 1
        kk = min(k, n_avail) if k > 0 else min(n_avail, 4096)
        for off in random.sample(range(n_avail), kk):
            out.append(str(ipaddress.IPv4Address(lo + off)))
    return out


def _sample_v6(net, k, total_limit=2000):
    """
    v6 固定到 /48 粒度，纯整数运算。

    CF 的 IPv6 网段极大（如 2400:cb00::/32 含 65536 个 /48），
    全展开会产生数百万地址，实际没人用得上。因此对每个网段做
    配额限制：该网段最多产出 total_limit 个地址（按块均分）。

    注意 total_limit 是「单个网段」的上限；要控制全部 v6 的总量，
    需在 expand_and_sample 里跨网段分配。
    """
    out = []
    if net.prefixlen >= 48:
        block_bits = net.prefixlen
        count_blocks = 1
    else:
        block_bits = 48
        count_blocks = 1 << (48 - net.prefixlen)

    # 每个块取几个：让总量不超过 total_limit
    per_block = k
    if count_blocks * k > total_limit:
        per_block = max(1, total_limit // count_blocks)
        # 若块太多导致每块不足 1 个，则只随机挑部分块
        if per_block < 1:
            per_block = 1
            count_blocks = min(count_blocks, total_limit)

    base = int(net.network_address)
    blk_size = 1 << (128 - block_bits)
    host_mask = blk_size - 1

    block_indices = list(range(count_blocks))
    if count_blocks * per_block > total_limit:
        random.shuffle(block_indices)
        block_indices = block_indices[:total_limit]

    for i in block_indices:
        blk_base = (base + i * blk_size) & ~host_mask
        for _ in range(per_block):
            addr = blk_base | (random.getrandbits(128 - block_bits) & host_mask)
            out.append(str(ipaddress.IPv6Address(addr)))
    return out


def _sample_v6_global(nets, k, total_limit):
    """
    跨网段地采样 IPv6，让总量（而非单网段）不超过 total_limit。

    按网段大小比例分配配额：大网段（如 2400:cb00::/32 含 65536 个 /48）
    少取，小网段（如 2606:4700::/32 也很大，但 2a06:98c1::/29 较小）多取，
    避免总量被单个巨网段独吞。最后统一 shuffle 后截断，保证精确不超限。
    """
    # 每个网段的 /48 块数（代表其"容量"）
    sizes = []
    for net in nets:
        if net.prefixlen >= 48:
            sizes.append(1)
        else:
            sizes.append(1 << (48 - net.prefixlen))
    total_size = sum(sizes)

    out = []
    # 按容量比例分配配额，但每个网段至少保底 1 个（若有余量）
    remaining = total_limit
    quotas = []
    for size in sizes:
        q = max(1, int(size / total_size * total_limit)) if total_size > 0 else 0
        q = min(q, size)
        quotas.append(q)
    # 若按比例算出的总和超出，从大网段开始往下削
    while sum(quotas) > total_limit:
        # 找配额最多的网段削 1 个
        idx = quotas.index(max(quotas))
        if quotas[idx] <= 1:
            break
        quotas[idx] -= 1
    # 若总量还没用满，按容量比例补足（小块优先，因为小块配额低）
    while sum(quotas) < total_limit:
        added = False
        for i, (q, s) in enumerate(zip(quotas, sizes)):
            if q < s:
                quotas[i] += 1
                added = True
                if sum(quotas) >= total_limit:
                    break
        if not added:
            break

    for net, q in zip(nets, quotas):
        if q <= 0:
            continue
        out.extend(_sample_v6(net, k, total_limit=q))

    random.shuffle(out)
    return out[:total_limit]


# ─── 3. GeoIP ────────────────────────────────────────────────────────────────

class GeoIP:
    """优先 maxminddb 库；缺失则返回空国家码。"""

    def __init__(self, mmdb_path):
        self.reader = None
        self._cache = {}
        if not mmdb_path or not os.path.exists(mmdb_path):
            log(f"      GeoIP db not found: {mmdb_path}")
            return
        try:
            import maxminddb
            self.reader = maxminddb.open_database(mmdb_path)
            log(f"      GeoIP loaded: {mmdb_path}")
        except ImportError:
            log("      maxminddb not installed, GeoIP fields empty (pip install maxminddb)")
        except Exception as e:
            log(f"      GeoIP load failed: {e}")

    def country(self, ip):
        if self.reader is None:
            return ""
        hit = self._cache.get(ip)
        if hit is not None:
            return hit
        cc = ""
        try:
            rec = self.reader.get(ip)
            if rec:
                c = rec.get("country") or rec.get("registered_country") or {}
                cc = (c.get("iso_code") or "").upper()
        except Exception:
            cc = ""
        self._cache[ip] = cc
        return cc

    def close(self):
        try:
            if self.reader:
                self.reader.close()
        except Exception:
            pass


# ─── 4. 并发探测 ─────────────────────────────────────────────────────────────

_lock = threading.Lock()
_prog = {"done": 0, "alive": 0}


def _bump(done=0, alive=0):
    with _lock:
        _prog["done"] += done
        _prog["alive"] += alive


def probe_ip(ip, port, want_trace=True, retries=2):
    """
    TCP 连接 + (可选) TLS + /cdn-cgi/trace 取 colo。

    注意: 高并发下 TLS 握手可能因本地资源耗尽而失败，
    因此对「TCP 通但没读到 colo」的情况做重试。
    """
    res = {"ip": ip, "port": port, "alive": False, "colo": "", "tls": False}

    try:
        for attempt in range(retries + 1):
            raw = None
            try:
                raw = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
                raw.settimeout(READ_TIMEOUT)
                res["alive"] = True

                if not want_trace:
                    return res

                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    tls = ctx.wrap_socket(raw, server_hostname=TRACE_HOST)
                except Exception:
                    tls = None

                if tls is not None:
                    res["tls"] = True
                    req = (
                        f"GET {TRACE_PATH} HTTP/1.1\r\n"
                        f"Host: {TRACE_HOST}\r\n"
                        f"User-Agent: {USER_AGENT}\r\n"
                        f"Accept: */*\r\nConnection: close\r\n\r\n"
                    )
                    tls.sendall(req.encode())
                    buf = b""
                    while len(buf) < 8192:
                        chunk = tls.recv(8192)
                        if not chunk:
                            break
                        buf += chunk
                    for line in buf.decode("utf-8", errors="replace").splitlines():
                        if line.startswith("colo="):
                            res["colo"] = line.split("=", 1)[1].strip()
                            break
                    try:
                        tls.close()
                    except Exception:
                        pass

                if res["colo"]:
                    return res                  # 拿到 colo
                if attempt < retries:
                    time.sleep(0.2)             # 退避后重试
                # 重试耗尽则继续，最终返回 res

            except Exception:
                if attempt >= retries:
                    break
            finally:
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:
                        pass

        return res if res["alive"] else None
    finally:
        _bump(done=1, alive=1 if res["alive"] else 0)


def probe_all(ips, port, workers, want_trace=True, label=""):
    log(f"[4/5] Probing {len(ips)} IPs (port {port}, workers {workers}) {label}")
    _prog["done"] = 0
    _prog["alive"] = 0
    results = []
    last = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe_ip, ip, port, want_trace) for ip in ips]
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r and r["alive"]:
                results.append(r)
            if time.time() - last >= 10:
                with _lock:
                    d, a = _prog["done"], _prog["alive"]
                log(f"      progress {d}/{len(ips)} ({d*100/max(1,len(ips)):.1f}%)  alive {a}")
                last = time.time()

    log(f"      done: alive {len(results)} / {len(ips)}")
    return results


# ─── 5. 输出 ─────────────────────────────────────────────────────────────────

def _now_cn():
    """当前北京时间（UTC+8）。CI 与本地都用它，保证时间戳一致。
    不依赖 tzdata，直接拿 UTC 加 8 小时，避免 Windows/精简容器没有时区库。"""
    return time.gmtime(time.time() + 8 * 3600)


def _stamp_cn():
    """北京时间字符串，例如 2026-10-16 14:30:05"""
    return time.strftime("%Y-%m-%d %H:%M:%S", _now_cn())


def _split_versions(rows):
    """把 rows 按 IPv4 / IPv6 分开。"""
    v4 = [r for r in rows if ":" not in r["ip"]]
    v6 = [r for r in rows if ":" in r["ip"]]
    return v4, v6


def _write_list(path, rows, stamp=None, kind=""):
    """写 IP:端口#国家 列表文件。

    首行是注释头（以 # 开头），记录生成时间/行数/视角。
    下游解析器都会跳过 # 开头的行，所以不影响使用，
    但打开文件就能看到「这批数据是什么时候出的」。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if stamp:
            tag = f" {kind}" if kind else ""
            f.write(f"# Cloudflare IP Pool{tag} | {stamp} (UTC+8) | "
                    f"{len(rows)} IPs | IP:Port#CC\n")
        for r in rows:
            f.write(f"{r['ip']}:{r['port']}#{r['country']}\n")


def write_outputs(results, geo, outdir, country_mode="colo", drop_unknown=True):
    """
    country_mode:
      colo  - 用 CF 机房(colo)推算国家【默认，最贴近实际】
      geoip - 用 IP 注册地（GeoIP）
    drop_unknown:
      True  - 只保留能确定国家的 IP（推荐，数据干净）
      False - 未知国家标 #XX 也保留
    """
    log("[5/5] Writing outputs ...")
    os.makedirs(outdir, exist_ok=True)

    seen = set()
    # 先按 IP 归组：多端口探测时同一 IP 会有多条结果（如 443 和 8443 都通）
    by_ip = {}
    for r in results:
        if not r.get("alive"):
            continue
        by_ip.setdefault(r["ip"], []).append(r)

    rows = []
    dropped = 0
    for ip, group in by_ip.items():
        # 每个 IP 只保留一条，端口从存活端口里随机挑一个
        r = random.choice(group)
        key = (r["ip"], r["port"])
        if key in seen:
            continue
        seen.add(key)

        colo = (r.get("colo") or "").upper()
        cc_from_colo = colo_country(colo)
        cc_geo = geo.country(r["ip"])

        if country_mode == "colo":
            cc = cc_from_colo
        else:
            cc = cc_geo

        if not cc and drop_unknown:
            dropped += 1
            continue

        rows.append({
            "ip": r["ip"],
            "port": r["port"],
            "country": cc or "XX",       # 主字段：国家
            "colo": colo,                # CF 机房代码
            "country_geoip": cc_geo,     # 备查：IP 注册地
            "ver": 6 if ":" in r["ip"] else 4,
        })

    mode_name = "CF colo" if country_mode == "colo" else "GeoIP"
    log(f"      country mode: {mode_name}")
    if dropped:
        log(f"      dropped {dropped} IPs with unknown country")

    rows.sort(key=lambda x: (x["country"], x["colo"], x["ip"]))
    v4_rows, v6_rows = _split_versions(rows)

    # 统一时间戳：文件头、meta.json、提交信息都用它（北京时间 UTC+8）
    stamp = _stamp_cn()

    all_txt     = os.path.join(outdir, "all.txt")
    v4_txt      = os.path.join(outdir, "ipv4.txt")
    v6_txt      = os.path.join(outdir, "ipv6.txt")
    by_colo_txt = os.path.join(outdir, "all_by_colo.txt")
    full_csv    = os.path.join(outdir, "all_full.csv")
    meta_json   = os.path.join(outdir, "meta.json")

    # 主输出: IP:端口#国家（v4 + v6 混合）
    _write_list(all_txt, rows, stamp, "ALL")

    # 分版本输出
    _write_list(v4_txt, v4_rows, stamp, "IPv4")
    _write_list(v6_txt, v6_rows, stamp, "IPv6")

    # 按机房分组: IP:端口#国家  但排序更直观（同机房聚在一起）
    with open(by_colo_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Cloudflare IP Pool BY-COLO | {stamp} (UTC+8) | "
                f"{len(rows)} IPs | IP:Port#CC (COLO)\n")
        for r in sorted(rows, key=lambda x: (x["colo"], x["ip"])):
            f.write(f"{r['ip']}:{r['port']}#{r['country']}  ({r['colo']})\n")

    # 明细（CSV 不加注释头，否则会破坏表头行的解析）
    with open(full_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ip", "port", "country", "colo",
                                          "country_geoip", "ver"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 统计
    colo_cnt = Counter(r["colo"] for r in rows if r["colo"])
    cc_cnt = Counter(r["country"] for r in rows)
    cc_v4 = Counter(r["country"] for r in v4_rows)
    cc_v6 = Counter(r["country"] for r in v6_rows)

    with open(meta_json, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": stamp + " UTC+8",
            "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "country_mode": mode_name,
            "total": len(rows),
            "total_ipv4": len(v4_rows),
            "total_ipv6": len(v6_rows),
            "countries": dict(cc_cnt.most_common()),
            "countries_ipv4": dict(cc_v4.most_common()),
            "countries_ipv6": dict(cc_v6.most_common()),
            "colo_top": dict(colo_cnt.most_common(50)),
            "vantage": {
                "note": "colo depends on the prober's network (CF is Anycast). "
                        "These colo values reflect the machine that ran this probe, "
                        "and may differ from your own network.",
                "timezone": "UTC+8 (Asia/Shanghai)",
                "dominant_colos": [c for c, _ in colo_cnt.most_common(5)],
            },
        }, f, ensure_ascii=False, indent=2)

    log(f"      stamp: {stamp} UTC+8")
    log(f"      {all_txt}   ({len(rows)} lines)")
    log(f"      {v4_txt}    ({len(v4_rows)} lines)")
    log(f"      {v6_txt}    ({len(v6_rows)} lines)")
    log(f"      {by_colo_txt}  (grouped by colo)")
    log(f"      {full_csv}  ({len(rows)} lines)")
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
    主流程：CLI 参数解析 → 配置合并 → 拉网段 → 采样 → 探测 → 输出。
    """
    ap = argparse.ArgumentParser(
        description="Build Cloudflare IP pool with country tags",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="可用 --init-config 生成默认 config.json 模板")
    ap.add_argument("--config", type=str, default="config.json",
                    help="配置文件路径 (默认 config.json，不存在则用内置默认)")
    ap.add_argument("--init-config", action="store_true",
                    help="生成默认 config.json 模板后退出")
    ap.add_argument("--sample", type=int, default=None, help="IPv4 每个 /24 取几个 IP (默认 3)")
    ap.add_argument("--sample-v6", type=int, default=None, help="IPv6 每个 /48 取几个 IP (默认 1)")
    ap.add_argument("--v6-limit", type=int, default=None, help="IPv6 总量上限 (默认 5000)")
    ap.add_argument("--ports", type=str, default=None, help="端口，逗号分隔 (默认 443,8443)")
    ap.add_argument("--workers", type=int, default=None, help="并发数 (默认 200)")
    ap.add_argument("--outdir", type=str, default=None, help="输出目录")
    ap.add_argument("--mmdb", type=str, default=None, help="GeoIP mmdb 路径")
    ap.add_argument("--no-v6", action="store_true", help="跳过 IPv6")
    ap.add_argument("--no-trace", action="store_true", help="只测连通性，不读 colo（更快）")
    ap.add_argument("--only-alive", action="store_true", help="只输出存活 IP（默认就是）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--country-mode", choices=["colo", "geoip"], default=None,
                    help="国家判定: colo=CF机房(推荐,默认) / geoip=IP注册地")
    ap.add_argument("--keep-unknown", action="store_true",
                    help="保留无法确定国家的 IP（默认丢弃）")
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

    # 配置优先级：内置默认 < config.toml < 命令行显式参数
    # 用 _cli_overrides 扫 argv 判断「用户到底给了哪些」，避免 None 歧义
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        cfg.update(load_config(args.config))
    given = _cli_overrides(args)

    if "sample" in given:
        cfg["sample"] = args.sample
    if "sample_v6" in given:
        cfg["sample_v6"] = args.sample_v6
    if "v6_limit" in given:
        cfg["v6_limit"] = args.v6_limit
    if "ports" in given:
        cfg["ports"] = [p.strip() for p in (args.ports or "").split(",") if p.strip()]
    if "workers" in given:
        cfg["workers"] = args.workers
    if "outdir" in given:
        cfg["outdir"] = args.outdir
    if "mmdb" in given:
        cfg["mmdb"] = args.mmdb
    if "seed" in given:
        cfg["seed"] = args.seed
    if "country_mode" in given:
        cfg["country_mode"] = args.country_mode
    if args.no_v6:
        cfg["no_v6"] = True
    if args.no_trace:
        cfg["no_trace"] = True
    if args.keep_unknown:
        cfg["keep_unknown"] = True

    if cfg["seed"] is not None:
        random.seed(cfg["seed"])

    t0 = time.time()
    log("=" * 60)
    log("CF IP Pool Builder")
    if args.config and os.path.exists(args.config):
        log(f"config: {args.config}")
    log("=" * 60)
    log(f"sample={cfg['sample']} sample_v6={cfg['sample_v6']} v6_limit={cfg['v6_limit']} "
        f"ports={cfg['ports']} workers={cfg['workers']} "
        f"no_v6={cfg['no_v6']} country_mode={cfg['country_mode']} "
        f"outdir={cfg['outdir']}")

    # 1. 拉取官方 CIDR（ips-v4 + ips-v6）
    cidrs = fetch_official_cidrs()
    if cfg["no_v6"]:
        cidrs[6] = []

    # 2. 拆子网 + 随机采样
    ipv4_ips, ipv6_ips = expand_and_sample(cidrs, cfg["sample"], cfg["sample_v6"], cfg["v6_limit"])
    all_ips = ipv4_ips + ipv6_ips
    if not all_ips:
        log("no IPs sampled, exit")
        sys.exit(1)

    # 3. 加载 GeoIP 数据库（可选）
    log("[3/5] Loading GeoIP ...")
    geo = GeoIP(cfg["mmdb"])

    ports = [int(p) for p in cfg["ports"]]
    primary = ports[0]

    # 4. 探测第一个端口（主端口）
    results = probe_all(all_ips, primary, cfg["workers"],
                        want_trace=not cfg["no_trace"])

    # 4b. 如果有多个端口，对存活 IP 补测其余端口
    if len(ports) > 1 and results:
        alive_ips = [r["ip"] for r in results]
        for p in ports[1:]:
            results.extend(probe_all(alive_ips, p, cfg["workers"],
                                     want_trace=not cfg["no_trace"],
                                     label=f"(extra port {p})"))

    # 5. 输出结果
    rows = write_outputs(results, geo, cfg["outdir"],
                         country_mode=cfg["country_mode"],
                         drop_unknown=not cfg["keep_unknown"])
    geo.close()

    # IPv6 存活为 0 时的提示（通常是当前网络无 IPv6 出网能力）
    if ipv6_ips and not any(":" in r["ip"] for r in results):
        log("")
        log("! WARN: no IPv6 results at all.")
        log("    Likely cause: this machine/network has no IPv6 egress.")
        log("    GitHub Actions runner has IPv6, so cloud runs produce v6 results.")
        log("    To skip IPv6 locally: set no_v6 = true in config.toml")
        log("    or pass --no-v6. Saves ~40% runtime.")

    log("=" * 60)
    log(f"finished in {time.time()-t0:.1f}s")
    log(f"  sampled {len(all_ips)} -> alive {len(rows)}")
    if rows:
        cnt = Counter(r["country"] or "XX" for r in rows)
        log(f"  countries: {', '.join(f'{k}:{v}' for k,v in cnt.most_common(12))}")
        colo_cnt = Counter(r["colo"] for r in rows if r["colo"])
        log(f"  colos: {', '.join(f'{k}:{v}' for k,v in colo_cnt.most_common(10))}")
    log("=" * 60)


if __name__ == "__main__":
    main()
