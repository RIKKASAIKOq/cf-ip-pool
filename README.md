# Cloudflare IP Pool

自动生成带国家标签的 Cloudflare IP 池，每 6 小时刷新一次。

## 产出

| 文件 | 说明 |
|---|---|
| `output/all.txt` | **主输出**，格式 `IP:端口#国家`，如 `104.23.207.194:443#HK` |
| `output/ipv4.txt` | 仅 IPv4 |
| `output/ipv6.txt` | 仅 IPv6（当前网络无 IPv6 出网时为空文件） |
| `output/all_by_colo.txt` | 按 CF 机房分组，带机房注释，如 `104.23.207.194:443#HK  (HKG)` |
| `output/all_full.csv` | 明细：`ip,port,country,colo,country_geoip,ver` |
| `output/meta.json` | 元信息：生成时间、总数、国家/机房分布 |

## 配置

参数通过 `config.toml` 管理，**命令行优先**：

```bash
python scripts/build_pool.py --init-config    # 生成 config.toml 模板
python scripts/build_pool.py                  # 读 config.toml
python scripts/build_pool.py --sample 7       # 覆盖 sample
python scripts/build_pool.py --config other.toml
```

也支持 `.json`（自动识别扩展名，兼容 UTF-8 BOM）。

`config.toml` 完整选项：

```toml
sample = 3
ports = [443]
workers = 200
outdir = "output"
mmdb = "GeoLite2-Country.mmdb"
no_v6 = false
no_trace = false
country_mode = "colo"
keep_unknown = false
seed = null
```

| 键 | 默认 | 说明 |
|---|---|---|
| `sample` | 3 | 每个 /24 或 /48 取几个 IP |
| `ports` | `[443]` | 端口列表，可写 `[443, 8443]` |
| `workers` | 200 | 并发数（过高会丢 colo，200~300 稳） |
| `outdir` | `output` | 输出目录 |
| `mmdb` | `GeoLite2-Country.mmdb` | GeoIP 库路径 |
| `no_v6` | `false` | 跳过 IPv6。**本机无 IPv6 出网时设 `true`，省 40% 时间** |
| `no_trace` | `false` | 只测连通性，不读 colo |
| `country_mode` | `colo` | `colo`=CF机房（推荐） / `geoip`=IP注册地 |
| `keep_unknown` | `false` | 保留无法确定国家的 IP |
| `seed` | `null` | 填整数可复现结果 |

> `no_v6` 默认 `false`，因为 GitHub runner 有 IPv6。只有本地跑才需要手动改成 `true`。

## 国家是怎么判定的（重要）

**默认用 CF 机房（colo）推算国家**，不是 IP 注册地。两者差别很大：

| | GeoIP `country_geoip` | CF `colo` |
|---|---|---|
| 含义 | IP **注册在**哪个国家 | **实际服务你的是哪个机房** |
| 实测对比 | — | 87.6% 的 IP 两者不一致 |
| 对你的意义 | 几乎没用 | **决定延迟和线路** |

Cloudflare 是 Anycast 架构，同一个 IP 在各地都会就近接入不同机房。所以
「IP 属于哪国」本身没有唯一答案，有意义的只有「**从你的网络过去落到哪个机房**」。

用 `country_mode = "geoip"` 可切换回注册地判定。

## ⚠️ 机房有「视角」问题

`colo` 是**从探测者所在网络测出来**的：

- 你在中国跑 → 多数落到 `HKG` / `SIN`
- GitHub Actions 在美国跑 → 多数落到 `LAX` / `SJC` / `IAD`
- 用户在日本跑 → 多数落到 `NRT`

**所以：如果你要按国家筛选，最好在自己网络里跑一遍。**
GitHub 版适合当「可用 IP 大全」，不适合当「你这儿的优选 IP」。

`output/meta.json` 的 `vantage` 字段记录了本次探测的视角（Top 5 机房）。

## 直链（仓库发布后可用）

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/all.txt
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/ipv4.txt
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/ipv6.txt
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/all_full.csv
```

## 原理

```
① 拉官方 ips-v4 / ips-v6
② 拆 /24(v4) 与 /48(v6)，每段随机取 N 个 IP
③ 并发 TCP+TLS 探测，读 speed.cloudflare.com/cdn-cgi/trace 拿 colo(机房)
④ colo -> 国家（内置 200+ 机房映射表）；GeoIP 作为备查字段
⑤ 输出 all.txt（混合）+ ipv4.txt / ipv6.txt（分版本）
```

**注意**：这里跑的是「连通性 + 归属地」，**不测速**。因为 GitHub 出口在美国，测出来的延迟跟你的实际网络无关。

## 手动触发

Actions 页面 → Build CF IP Pool → Run workflow，可自定义：

- `sample`：每个子网取几个 IP（默认走 config.toml）
- `ports`：端口，逗号分隔
- `no_v6`：跳过 IPv6（`true`/`false`）
- `country_mode`：`colo`（推荐）/ `geoip`

手动输入会覆盖 `config.toml` 对应项；留空则用 `config.toml` 的值。

## 本地运行

```bash
pip install maxminddb
curl -sSL -o GeoLite2-Country.mmdb \
  https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-Country.mmdb

python scripts/build_pool.py --init-config
# 编辑 config.toml，本机无 IPv6 就把 no_v6 改成 true
python scripts/build_pool.py
```

## 实测数据（本地，sample=3，本机无 IPv6）

```
采样 31868 → 存活 14855（ipv4 14855 / ipv6 0）→ 耗时 250s

国家: HK 6415 / SG 5022 / US 684 / MY 163 / ES 77 / DE 63 / JP 59 / FR 58 ...
机房: HKG 6415 / SIN 5022 / KUL 163 / LAX 151 / SJC 135 / EWR 108 / IAD 87 ...
```

另一次带 IPv6 探测（本机无 v6 出网）跑完 31868 个需 445s，其中 v6 全部失败。

## 说明

- 无 `maxminddb` 时脚本仍能跑，只是 GeoIP 字段为空（colo 判定不受影响）。
- GeoIP 库不进仓库（3.4 MB），Actions 里现下。
- 所有 IP 均来自 Cloudflare 官方公布网段。
- IPv6 需要运行环境支持 IPv6 出网（GitHub runner 有，部分家庭宽带没有）。
