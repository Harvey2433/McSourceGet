"""全局配置：缓存目录、网络端点、工具版本与下载地址。"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 缓存目录（jar、mappings、下载的工具都放这里，避免重复下载）
# ---------------------------------------------------------------------------
CACHE_DIR = Path(os.environ.get("MCSG_CACHE", Path.home() / ".mcsourceget"))
JAR_CACHE = CACHE_DIR / "jars"
MAP_CACHE = CACHE_DIR / "mappings"
TOOL_CACHE = CACHE_DIR / "tools"
WORK_DIR = CACHE_DIR / "work"

for _d in (CACHE_DIR, JAR_CACHE, MAP_CACHE, TOOL_CACHE, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 网络端点
# ---------------------------------------------------------------------------
VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

FABRIC_META = "https://meta.fabricmc.net/v2"
FABRIC_MAVEN = "https://maven.fabricmc.net"

# Legacy Fabric：社区为老版本（1.3~1.13）逐版本生成的 intermediary + yarn。
# 这些映射是按字节码结构匹配生成的版本专属映射（稳定名），不是套邻版的 notch 名，
# 接口与官方 Fabric 完全同构（meta v2 + maven），故 Legacy Yarn 复用 Yarn 同一条管线。
LEGACY_FABRIC_META = "https://meta.legacyfabric.net/v2"
LEGACY_FABRIC_MAVEN = "https://repo.legacyfabric.net/legacyfabric"

# OrnitheMC：覆盖 pre-classic ~ 1.14.4 的最完整老版本映射体系，逐版本生成。
#   - Calamus = intermediary（official->intermediary，跨版本稳定名），meta 端点名为 intermediary
#   - Feather = 可读名层（intermediary->named），对标 Yarn
# 两步重映射：official->intermediary(calamus) + intermediary->named(feather)，复用 yarn 管线。
ORNITHE_META = "https://meta.ornithemc.net/v3"
ORNITHE_MAVEN = "https://maven.ornithemc.net/releases"

# Forge maven：经典 MCP 的 SRG（notch->searge）与 CSV（searge->可读名）件源
FORGE_MAVEN = "https://maven.minecraftforge.net"
# SRG：de/oceanlabs/mcp/mcp/<ver>/mcp-<ver>-srg.zip（内含 joined.srg）
MCP_SRG_URL = FORGE_MAVEN + "/de/oceanlabs/mcp/mcp/{ver}/mcp-{ver}-srg.zip"
# Forge maven 上「确实发布过 SRG」的版本（取自 mcp 的 maven-metadata.xml）。
# SRG 是 notch->searge 的版本专属映射，缺失的点版本（如 1.7.4~1.7.9、1.8.1~1.8.7、
# 1.9.1/1.9.3、1.10.1 等）从未发布，且不能用邻版顶替，故经典 MCP 仅对这些版本可用。
# 1.6.4 虽在列，但归 MCP(Legacy) 处理，这里不含。
MCP_SRG_VERSIONS = frozenset({
    "1.7.2", "1.7.10",
    "1.8", "1.8.8", "1.8.9",
    "1.9", "1.9.2", "1.9.4",
    "1.10", "1.10.2",
    "1.11", "1.11.1", "1.11.2",
    "1.12", "1.12.1", "1.12.2",
})
# CSV：两个频道，stable 优先、snapshot 兜底；版本号形如 39-1.12 / 20180101-1.12
MCP_CSV_CHANNELS = ("mcp_stable", "mcp_snapshot")
MCP_CSV_METADATA = FORGE_MAVEN + "/de/oceanlabs/mcp/{channel}/maven-metadata.xml"
MCP_CSV_URL = FORGE_MAVEN + "/de/oceanlabs/mcp/{channel}/{cver}/{channel}-{cver}.zip"

# MCP(Legacy)：≤1.6.4 的老版本 Forge maven 上从未发布 SRG，改用 RetroMCP 体系自带的
# Tiny V2 映射（official/client -> named，混淆名直接到可读名，与 Yarn 同管线）。
# 精准映射「Mojang 清单版本 id -> mappings zip 下载地址」，只有这些版本有现成映射：
#   1.0.0~1.2.5、1.5.2 来自 mcphackers.org；1.6.4 来自 RetroMCP-Legacy 仓库。
# 注意 Mojang 清单里 1.0 的 id 是 "1.0"，而 mcphackers 的包名是 1.0.0.zip。
_MCPHACKERS_V2 = "https://mcphackers.org/versionsV2"
_RETROMCP_LEGACY_RAW = (
    "https://raw.githubusercontent.com/ReSpouted/RetroMCP-Legacy/main"
    "/src/main/resources/mappings/versionsV2"
)
MCP_LEGACY_MAPPINGS = {
    "1.0":   f"{_MCPHACKERS_V2}/1.0.0.zip",
    "1.1":   f"{_MCPHACKERS_V2}/1.1.zip",
    "1.2.3": f"{_MCPHACKERS_V2}/1.2.3.zip",
    "1.2.4": f"{_MCPHACKERS_V2}/1.2.4.zip",
    "1.2.5": f"{_MCPHACKERS_V2}/1.2.5.zip",
    "1.5.2": f"{_MCPHACKERS_V2}/1.5.2.zip",
    "1.6.4": f"{_RETROMCP_LEGACY_RAW}/1.6.4.zip",
}

# ---------------------------------------------------------------------------
# 自动下载的工具（反编译器 / 反混淆器）
# ---------------------------------------------------------------------------
# Vineflower：反编译器（fork 自 FernFlower，对新 Java 特性支持更好）
VINEFLOWER_VERSION = "1.12.0"
VINEFLOWER_URL = (
    "https://gh-proxy.org/https://github.com/Vineflower/vineflower/releases/download/"
    f"{VINEFLOWER_VERSION}/vineflower-{VINEFLOWER_VERSION}.jar"
)
VINEFLOWER_JAR = TOOL_CACHE / f"vineflower-{VINEFLOWER_VERSION}.jar"

# tiny-remapper：按 tiny 格式 mappings 重映射字节码（Yarn 路线用）
TINY_REMAPPER_VERSION = "0.10.4"
TINY_REMAPPER_URL = (
    f"{FABRIC_MAVEN}/net/fabricmc/tiny-remapper/{TINY_REMAPPER_VERSION}/"
    f"tiny-remapper-{TINY_REMAPPER_VERSION}-fat.jar"
)
TINY_REMAPPER_JAR = TOOL_CACHE / f"tiny-remapper-{TINY_REMAPPER_VERSION}-fat.jar"

# SpecialSource：按 ProGuard/tsrg 重映射字节码（Mojang 官方 mapping 路线用）
SPECIALSOURCE_VERSION = "1.11.4"
SPECIALSOURCE_URL = (
    "https://repo1.maven.org/maven2/net/md-5/SpecialSource/"
    f"{SPECIALSOURCE_VERSION}/SpecialSource-{SPECIALSOURCE_VERSION}-shaded.jar"
)
SPECIALSOURCE_JAR = TOOL_CACHE / f"SpecialSource-{SPECIALSOURCE_VERSION}-shaded.jar"

# ---------------------------------------------------------------------------
# 反编译并发与内存控制
#   - 每个反编译/重映射任务会 fork 一个 JVM。JVM 默认最大堆 = 物理内存的 1/4，
#     多个并发会瞬间申请远超物理内存的提交内存，撑爆 Windows 页面文件
#     （报错：os::commit_memory ... 页面文件太小）。
#   - 因此：① 限制同时运行的反编译任务数；② 给每个 JVM 显式 -Xmx 上限。
# ---------------------------------------------------------------------------
# 同时反编译的版本数（每个约 -Xmx 上限，峰值内存 ≈ 该值 × DECOMPILE_MAX_HEAP）
DECOMPILE_CONCURRENCY = int(os.environ.get("MCSG_CONCURRENCY", "4"))
# 单个反编译 JVM 的最大堆
DECOMPILE_MAX_HEAP = os.environ.get("MCSG_MAX_HEAP", "2g")

# ---------------------------------------------------------------------------
# 下载并发与限流
#   - 全局并发可以开大（多主机并行更快），但「同一主机」的并发必须收着：
#     客户端 jar 全在 Mojang 一个 CDN 上，16 条流并发会触发它的 per-IP 限速
#     （55MB/s 瞬间掉到 KB，约 30s 后恢复）。按主机限并发即可既快又不被掐。
#   - 下载块设小一些：块越大，慢速/限速时 done 越久才更新一次，进度看着像卡住；
#     设成 256KB 让「剩余字节」实时往下走。
# ---------------------------------------------------------------------------
DOWNLOAD_CONCURRENCY = int(os.environ.get("MCSG_DOWNLOAD_CONCURRENCY", "12"))
DOWNLOAD_PER_HOST = int(os.environ.get("MCSG_DOWNLOAD_PER_HOST", "6"))
DOWNLOAD_CHUNK = 256 * 1024

# 网络请求统一 UA / 超时
HTTP_TIMEOUT = 60
HTTP_HEADERS = {"User-Agent": f"MCSourceDd/1.0 OpenMapleTerminal/1.0 (local research tool)"}

# ---------------------------------------------------------------------------
# 共享 HTTP 会话：自动重试 + 退避 + 连接池限流
#   - 大量并发请求曾把 fabricmc.net 打到 SSL/连接中断（且无重试直接失败）。
#   - 用一个带 urllib3 Retry 的 Session，对瞬时网络错误退避重试；
#     并限制连接池大小，避免对单一主机瞬间堆太多连接。
# ---------------------------------------------------------------------------
import requests as _requests
from requests.adapters import HTTPAdapter as _HTTPAdapter

try:
    from urllib3.util.retry import Retry as _Retry
except ImportError:  # 兼容旧版打包路径
    from requests.packages.urllib3.util.retry import Retry as _Retry

# 放大连接池以匹配高速 IO 下载的 16 线程极限并发
HTTP_POOL_SIZE = 16

_retry = _Retry(
    total=3,                 # 最多重试 3 次
    backoff_factor=1.0,      # 退避：1s, 2s, 4s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "HEAD"]),
    raise_on_status=False,
)
_adapter = _HTTPAdapter(
    max_retries=_retry,
    pool_connections=HTTP_POOL_SIZE,
    pool_maxsize=HTTP_POOL_SIZE,
)

SESSION = _requests.Session()
SESSION.headers.update(HTTP_HEADERS)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)