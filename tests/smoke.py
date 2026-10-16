"""冒烟测试：只用一小段 CF 网段验证 build_pool 的采样/探测/输出逻辑。

不会去拉官方 CIDR，也不打全量——就在 104.16.0.0/17 里跑，
产物写到 out_test/（被 .gitignore 忽略）。

    python tests/smoke.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_pool as bp  # noqa: E402

OUTDIR = os.path.join(ROOT, "out_test")

# 104.16.0.0/17 = 128 个 /24，只在这一小段里测，不打全量
RANGES = bp.host_ranges(["104.16.0.0/17"])
print(f"ranges: {len(RANGES)} blocks, capacity {bp.total_hosts(RANGES)}")
assert len(RANGES) == 128, len(RANGES)
assert bp.total_hosts(RANGES) == 128 * 254

cfg = dict(bp.DEFAULT_CONFIG)
cfg.update({
    "sample": 2,
    "target": 250,
    "workers": 200,
    "timeout": 2.0,
    "max_rounds": 4,
    "sample_step": 2,
    "seed": 20261017,
    "outdir": OUTDIR,
})

pairs, stats = bp.build_pool(RANGES, cfg)
rows = bp.write_outputs(pairs, cfg["outdir"], cfg)

print(f"\n=== RESULT: {len(rows)} rows ===")
assert stats["probed"] > 0, stats

# ---- 断言 ----
assert len(rows) == len(pairs), (len(rows), len(pairs))
assert len(rows) <= cfg["target"], len(rows)
assert all(r["ver"] == 4 for r in rows)
assert all(":" not in r["ip"] for r in rows)          # 无 IPv6
assert all(r["port"] in cfg["ports"] for r in rows)   # 端口都在加密端口池里
assert len({r["ip"] for r in rows}) == len(rows)      # IP 不重复
assert len({(r["ip"], r["port"]) for r in rows}) == len(rows)

# 端口应该分散在多个端口上（6 个端口随机分配）
ports_used = sorted({r["port"] for r in rows})
print(f"ports used: {ports_used}")
assert len(ports_used) >= 3, ports_used

# 排序按整数地址，不是字符串
ints = [int(__import__("ipaddress").ip_address(r["ip"])) for r in rows]
assert ints == sorted(ints), "not sorted numerically"

# 文件内容
v4_path = os.path.join(cfg["outdir"], "ipv4.txt")
with open(v4_path, encoding="utf-8") as f:
    lines = f.read().splitlines()
assert lines[0].startswith("# Cloudflare IP Pool IPv4"), lines[0]
body = [l for l in lines[1:] if l.strip()]
assert len(body) == len(rows), (len(body), len(rows))
assert all(l.count(":") == 1 and l.split(":")[1].isdigit() for l in body)
assert all("#" not in l for l in body), "不允许出现国家/机房标注"
print("first lines:", lines[:3])

with open(os.path.join(cfg["outdir"], "meta.json"), encoding="utf-8") as f:
    meta = json.load(f)
print("meta:", json.dumps(meta, ensure_ascii=False))
assert meta["total"] == len(rows)
assert meta["probe"]["mode"] == "tcp"
assert meta["probe"]["timeout"] == 2.0
assert meta["probe"]["workers"] == 200
assert set(meta["ports"]) <= {str(p) for p in cfg["ports"]}

# 只产出 ipv4.txt + meta.json
produced = sorted(f for f in os.listdir(cfg["outdir"]) if not f.startswith("_"))
print("outdir files:", produced)
assert produced == ["ipv4.txt", "meta.json"], produced

# header=False 时不该写注释头
bp._write_list(os.path.join(OUTDIR, "_hdr.txt"), rows, "X", "IPv4", header=False)
with open(os.path.join(OUTDIR, "_hdr.txt"), encoding="utf-8") as f:
    hl = f.read().splitlines()
assert len(hl) == len(rows), len(hl)
assert not any(l.startswith("#") for l in hl)
os.remove(os.path.join(OUTDIR, "_hdr.txt"))
print("header=False: OK")

print("\nALL SMOKE TESTS PASSED")
