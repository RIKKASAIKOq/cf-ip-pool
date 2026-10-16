# Cloudflare IP Pool

自动生成 Cloudflare 可用 IPv4 池，每 6 小时刷新一次。**纯 `IP:端口`，无任何国家/机房标注。**

## 产出

| 文件 | 说明 |
|---|---|
| `output/ipv4.txt` | **唯一输出**，格式 `IP:端口`，如 `104.23.207.194:443`。只收 TCP 探测通过的 IP |
| `output/meta.json` | 元信息边车：生成时间、条数、端口分布、探测参数（不是 IP 池文件） |

`ipv4.txt` 的**第 1 行是 `#` 开头的注释头**，记录生成时间（北京时间）、IP 数和格式：

```
# Cloudflare IP Pool IPv4 | 2026-10-17 02:30:05 (UTC+8) | 20000 IPs | IP:Port
104.23.207.194:443
```

下游解析器普遍跳过 `#` 行；若不想要这行，配置里 `"header": false` 或跑 `--no-header`。

不再产出 `all.txt` / `ipv6.txt` / `all_by_colo.txt` / `all_full.csv`，也没有国家、机房（colo）、GeoIP 字段。

## 配置

参数通过 `config.json` 管理，**命令行优先**：

```bash
python scripts/build_pool.py --init-config    # 生成 config.json 模板
python scripts/build_pool.py                  # 读 config.json
python scripts/build_pool.py --target 30000   # 覆盖 target
python scripts/build_pool.py --config other.json
```

也支持 `.toml`（自动识别扩展名，兼容 UTF-8 BOM）。

| 键 | 默认 | 说明 |
|---|---|---|
| `sample` | 6 | 每个 /24 随机取几个 IP（起始值）。凑不够 `target` 时自动按 `sample_step` 加码重采 |
| `ports` | `[443, 8443, 2053, 2083, 2087, 2096]` | 加密端口池。**每个 IP 随机分其中一个**，所以 6 个端口大致均匀混在结果里 |
| `workers` | 1000 | 并发线程数。**若当前 fd 上限装不下，会自动下调**（见下） |
| `timeout` | 2.0 | TCP 连接超时（秒）。连不上就当不可用，不干等 |
| `target` | 20000 | 目标存活条数（2w）。凑不够不报错，给多少算多少 |
| `sample_step` | 2 | 每轮补采样时，每 /24 取样数增加多少 |
| `max_rounds` | 6 | 最多补采样几轮 |
| `outdir` | `output` | 输出目录 |
| `header` | `true` | `ipv4.txt` 首行是否写 `#` 注释头；`false` = 纯 `IP:端口`（等价 `--no-header`） |
| `seed` | `null` | 填整数可复现采样和端口分配结果 |

> 探测是**纯 TCP 连通性**：不做 TLS 握手、不读 `cdn-cgi/trace`、不解析响应体。
> 连上即收，2 秒为限。要验 TLS 可用性请自行在客户端侧确认。

### 并发上限会自动适配 fd

每个在飞的探测占 1 个文件描述符。如果 `workers` 超过系统 `ulimit -n` 的 80%（并给系统留 64 个），
脚本会**自动下调并发**并打日志，不会闷头往前跑：

```
! workers 1000 -> 819 (fd limit 1024, reserved 64 for the system)
```

这是必要的：超限时连接会被 OS 拒绝，报出来是 `OSError: Too many open files`，
混在失败原因里跟「IP 不通」长得一样，会悄悄压低命中率。

Windows 没有 ulimit，`_max_fds()` 返回 `None`，此时不限制、按配置值跑。

想让 Linux 上跑满 1000 并发，把上限提上去就行，例如 `ulimit -n 65535` 后再运行。

## 原理

```
① 拉官方 ips-v4
② 拆 /24，每段随机取 N 个 IP（N 不够就逐轮加码）
③ 每个 IP 随机分配 1 个加密端口
④ 并发 TCP 探测，超时 2 秒（线程池，线程数按 fd 上限自动收敛）
⑤ 只把 TCP 通的 IP:端口 写进 output/ipv4.txt，凑满 target 即停
```

## 手动触发

Actions 页面 → Build CF IP Pool → Run workflow，可自定义：

- `sample`：每 /24 取样数
- `ports`：端口，逗号分隔
- `workers`：并发数
- `target`：目标条数
- `timeout`：TCP 超时秒数
- `no_header`：填 `true` 则 `ipv4.txt` 不写注释头，输出纯 `IP:端口`

手动输入会覆盖 `config.json` 对应项；留空则用配置值。

## 本地运行

```bash
python scripts/build_pool.py --init-config
# 编辑 config.json
python scripts/build_pool.py
```

无第三方依赖，标准库即可运行（不需要 `maxminddb`，不下载 GeoIP 库）。

## 测试

```bash
python tests/smoke.py
```

只在小网段（`104.16.0.0/17`）里跑 250 条，验证采样、探测、端口分配、输出格式，几秒完成；
产物写到 `out_test/`（已 gitignore）。改逻辑后先跑它，再跑全量。

## 直链（仓库发布后可用）

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/ipv4.txt
```

## 实测参考

本地（并发 1000、超时 2s、纯 TCP）跑 2 万条量级，分钟级完成；
实际耗时取决于本机到 CF 边缘的 RTT 和丢包。
