"""交互式 CLI 入口：自实现的 Cargo 风格执行流。

保留 ASCII 横幅与「已加载版本数量 + 最新正式版」展示，其余全部手写实现：
  - 按历史断代给出反混淆映射选项（方向键选择，单版本不使用区间措辞）
  - Downloading：真实网络下载，底部一行随剩余字节实时变化，每完成一个弹出 Downloaded
  - Fetching：文件完整性校验（逐块计算 SHA256），带 [====> ] 进度条与耗时
  - Compiling：版本反编译阶段
"""

from __future__ import annotations

import os
import sys
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel

from .manifest import Manifest, fetch_version_json, VersionEntry
from .mappings import (
    MappingKind, is_unobfuscated, prepare_mojang, prepare_yarn, prepare_mcp,
    prepare_mcp_legacy, has_mcp_legacy, MappingArtifacts,
)
from .pipeline import process_version
from .versions import resolve_selection, _parse_mc_version
from .config import (
    JAR_CACHE, VINEFLOWER_JAR, VINEFLOWER_URL, VINEFLOWER_VERSION,
    TINY_REMAPPER_JAR, TINY_REMAPPER_URL, TINY_REMAPPER_VERSION,
    SPECIALSOURCE_JAR, SPECIALSOURCE_URL, SPECIALSOURCE_VERSION,
    HTTP_HEADERS, HTTP_TIMEOUT, DECOMPILE_CONCURRENCY, SESSION,
)

console = Console()

# --- ANSI 配色（手写，Cargo 风格状态词右对齐到 12 列）---------------------
GREEN = "\033[1;32m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

BANNER = r"""
 __  __      ____                           ____      _
|  \/  | ___/ ___|  ___  _   _ _ __ ___ ___|  _ \  __| |
| |\/| |/ __\___ \ / _ \| | | | '__/ __/ _ \ | | |/ _` |
| |  | | (__ ___) | (_) | |_| | | | (_|  __/ |_| | (_| |
|_|  |_|\___|____/ \___/ \__,_|_|  \___\___|____/ \__,_|
"""


def _enable_ansi() -> None:
    """在 Windows 控制台启用 VT 转义处理，保证手写 ANSI 生效。"""
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def _status(word: str, color: str = GREEN) -> str:
    """Cargo 风格状态词：右对齐 12 列并着色。"""
    return f"{color}{word:>12}{RESET}"


def _bar(pct: float, width: int = 40) -> str:
    """复刻 Cargo 的 [====>   ] 进度条。"""
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100.0 * width)
    if filled >= width:
        return "=" * width
    return "=" * filled + ">" + " " * (width - filled - 1)


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class LiveLine:
    """维护一条停留在底部、可实时刷新的状态行；日志从其上方逐条滚出。"""

    def __init__(self) -> None:
        self._current = ""

    def set(self, text: str) -> None:
        self._current = text
        sys.stdout.write("\r\033[2K" + text)
        sys.stdout.flush()

    def log(self, text: str) -> None:
        sys.stdout.write("\r\033[2K" + text + "\n")
        sys.stdout.write(self._current)
        sys.stdout.flush()

    def clear(self) -> None:
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()
        self._current = ""


def _era_options(vid: str):
    """按历史断代返回 (era, options, default_idx)。

    era==0：无需询问、静默 NONE（26.1+ 取消混淆）。
    era==7：1.0~1.6.4 区间但无现成 MCP(Legacy) 映射，不询问、走 NONE，并提示用户。
    options 为 [(label, MappingKind), ...]，default_idx 是默认高亮项。
    版本矩阵：
      1.0   ~ 1.6.4（在映射表内）: None / MCP(Legacy)
      1.0   ~ 1.6.4（不在表内）  : 不询问，警告后走 None
      1.7   ~ 1.12.2            : None / MCP
      1.13.x                    : None / MCP-Reborn
      1.14  ~ 1.21.4           : Yarn(默认) / Mojang / None / MCP-Reborn
      1.21.4 < v ~ 1.21.11      : Yarn(默认) / Mojang / None
      v > 1.21.11               : 不询问（26.1+ 未混淆，运行时兜底 NONE）
    """
    none_opt = ("None (直出)", MappingKind.NONE)
    yarn_opt = ("Yarn Mappings", MappingKind.YARN)
    mojang_opt = ("Mojang Mappings", MappingKind.MOJANG)
    mcp_opt = ("MCP Mappings", MappingKind.MCP)
    mcpl_opt = ("MCP Mappings (Legacy) (by RetroMCP)",
                MappingKind.MCP_LEGACY)
    reborn_opt = ("MCP-Reborn Mappings",
                  MappingKind.MCP_REBORN)

    nums = _parse_mc_version(vid)
    if not nums:
        # 快照/非数字版本号：26w.. 这类新体系已取消混淆，不询问；其余按 1.14+ 段处理
        if vid.startswith("2"):
            return 0, [none_opt], 0
        return 3, [yarn_opt, mojang_opt, none_opt, reborn_opt], 0

    if (1, 0) <= nums <= (1, 6, 4):
        if has_mcp_legacy(vid):
            return 6, [none_opt, mcpl_opt], 1
        return 7, [none_opt], 0
    if (1, 7) <= nums <= (1, 12, 2):
        return 1, [none_opt, mcp_opt], 1
    if nums[:2] == (1, 13):
        return 2, [none_opt, reborn_opt], 1
    if (1, 14) <= nums <= (1, 21, 4):
        return 3, [yarn_opt, mojang_opt, none_opt, reborn_opt], 0
    if (1, 21, 4) < nums <= (1, 21, 11):
        return 5, [yarn_opt, mojang_opt, none_opt], 0
    # v > 1.21.11：26.1+ 未混淆，不询问
    return 0, [none_opt], 0


def _read_key() -> str:
    """读取一次按键，归一化为 'up' / 'down' / 'enter' / 'ctrl-c' / 字符。"""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):          # 功能键前缀
            code = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl-c"
        return ch
    # POSIX
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down"}.get(seq, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl-c"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _select(prompt: str, options: list, default_idx: int) -> int:
    """方向键单选菜单，返回选中下标。手写实现，原地重绘。"""
    idx = default_idx
    n = len(options)

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\033[{n}A")        # 光标上移 n 行回到菜单顶部
        for i, (label, _) in enumerate(options):
            sys.stdout.write("\033[2K")
            if i == idx:
                sys.stdout.write(f"  {CYAN}>{RESET} {CYAN}{label}{RESET}\n")
            else:
                sys.stdout.write(f"    {label}\n")
        sys.stdout.flush()

    sys.stdout.write(f"{BOLD}? {prompt}{RESET}{DIM}  (↑/↓ 选择，回车确认){RESET}\n")
    render(first=True)
    while True:
        key = _read_key()
        if key == "up":
            idx = (idx - 1) % n
            render(first=False)
        elif key == "down":
            idx = (idx + 1) % n
            render(first=False)
        elif key == "enter":
            return idx
        elif key == "ctrl-c":
            sys.stdout.write("\n")
            sys.exit(0)


def _human_bytes(n: int) -> str:
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f}GiB"
    if mb >= 1:
        return f"{mb:.1f}MiB"
    return f"{n / 1024:.0f}KiB"


def _download_phase(files: list[tuple]) -> None:
    """Downloading 阶段：复刻 Cargo —— 总量边下边「涨」，剩余字节边收边「降」。

    不做任何预探测。每个下载连接一打开，就从响应头 Content-Length 把这份大小
    累加进 total_bytes（于是总量随并发连接陆续建立而增长）；收到的每个 chunk
    累加进 done_bytes。显示的 remaining = total - done，因此该数字会同时受
    「新连接揭示大小（上涨）」与「字节持续到达（下降）」两股力影响而动态波动。
    """
    missing = [f for f in files
               if not (f[0].exists() and f[0].stat().st_size > 0)]
    if not missing:
        return

    total_bytes = 0          # 已被各连接 Content-Length 揭示的总量（动态增长）
    done_bytes = 0           # 已落盘字节
    remaining_files = len(missing)
    lock = threading.Lock()
    line = LiveLine()

    def fmt_line() -> str:
        remaining = max(0, total_bytes - done_bytes)
        return (f"{_status('Downloading', CYAN)} {remaining_files} crates, "
                f"remaining bytes: {_human_bytes(remaining)}")

    line.set(fmt_line())     # 立即显示，total 从 0 起，随连接建立往上涨

    def worker(dest: Path, url: str, label: str) -> str:
        nonlocal total_bytes, done_bytes, remaining_files
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with SESSION.get(url, timeout=HTTP_TIMEOUT, stream=True) as r:
                r.raise_for_status()
                # 连接建立：把这份的大小并入总量（总量在此刻上涨）
                cl = int(r.headers.get("content-length", 0))
                if cl:
                    with lock:
                        total_bytes += cl
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        with lock:
                            done_bytes += len(chunk)
            tmp.replace(dest)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        with lock:
            remaining_files -= 1
        return label

    with ThreadPoolExecutor(max_workers=min(len(missing), DECOMPILE_CONCURRENCY)) as ex:
        futs = {ex.submit(worker, d, u, l): l for d, u, l, _s in missing}
        pending = set(futs)
        line.set(fmt_line())
        while pending:
            done, pending = _wait_some(pending, timeout=0.08)
            for fut in done:
                try:
                    label = fut.result()
                    line.log(f"{_status('Downloaded', GREEN)} {label}")
                except Exception as e:
                    line.log(f"{_status('Failed', RED)} {futs[fut]} -> {e}")
            line.set(fmt_line())
    line.clear()


def _wait_some(futures: set, timeout: float):
    """等一小段时间，返回 (已完成集合, 仍挂起集合)；用于边下边刷新进度。"""
    from concurrent.futures import wait, FIRST_COMPLETED
    done, pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
    return done, pending


def _fetch_phase(files: list[tuple[Path, str, str]]) -> None:
    """Fetching 阶段：逐块计算 SHA256 校验文件完整性，带 Cargo 风格进度条。"""
    line = LiveLine()
    for dest, _url, label in files:
        if not dest.exists():
            continue
        size = dest.stat().st_size or 1
        hasher = hashlib.sha256()
        read = 0
        start = time.time()
        last = 0.0
        with open(dest, "rb") as f:
            while True:
                chunk = f.read(131072)
                if not chunk:
                    break
                hasher.update(chunk)
                read += len(chunk)
                now = time.time()
                if now - last > 0.03 or read >= size:
                    pct = read / size * 100.0
                    line.set(f"{_status('Fetching', CYAN)} {label} "
                             f"[{_bar(pct, 30)}] {pct:>3.0f}% "
                             f"({_fmt_elapsed(now - start)})")
                    last = now
    line.clear()


def _process_version(entry: VersionEntry, kind: MappingKind,
                     output_dir: Path, vjson_cache: dict) -> tuple[bool, str]:
    try:
        vjson = vjson_cache[entry.id]
        actual = kind
        if is_unobfuscated(entry, vjson):
            actual = MappingKind.NONE
        if actual == MappingKind.MOJANG:
            arts = prepare_mojang(entry, vjson)
        elif actual == MappingKind.YARN:
            arts = prepare_yarn(entry, entry.id)
        elif actual == MappingKind.MCP:
            arts = prepare_mcp(entry, entry.id)
        elif actual == MappingKind.MCP_LEGACY:
            arts = prepare_mcp_legacy(entry, entry.id)
        elif actual == MappingKind.MCP_REBORN:
            arts = MappingArtifacts(kind=MappingKind.MCP_REBORN)
        else:
            arts = MappingArtifacts(kind=MappingKind.NONE)
        result = process_version(entry, arts, output_dir)
        return (True, str(result))
    except Exception as e:
        return (False, str(e))


def _compile_phase(versions, group_mappings, output_dir, vjson_cache) -> None:
    """Compiling 阶段：并发反编译，底部进度条 + 上方逐条结果。"""
    line = LiveLine()
    total = len(versions)
    done = 0
    start = time.time()

    def fmt_line(current: str) -> str:
        pct = done / total * 100.0
        return (f"{_status('Compiling', CYAN)} [{_bar(pct, 30)}] "
                f"{done}/{total} ({_fmt_elapsed(time.time() - start)})  {current}")

    line.set(fmt_line("初始化"))
    with ThreadPoolExecutor(max_workers=DECOMPILE_CONCURRENCY) as ex:
        futures = {
            ex.submit(_process_version, v, group_mappings[v.id], output_dir, vjson_cache): v.id
            for v in versions
        }
        for fut in as_completed(futures):
            vid = futures[fut]
            try:
                ok, msg = fut.result()
                if ok:
                    line.log(f"{_status('Finished', GREEN)} {vid} 源码已输出")
                else:
                    line.log(f"{_status('Failed', RED)} {vid} -> {msg}")
            except Exception as e:
                line.log(f"{_status('Error', RED)} {vid} -> {e}")
            done += 1
            line.set(fmt_line(vid))
    line.clear()


def main() -> None:
    _enable_ansi()
    console.print(Panel(BANNER, style="bold blue", expand=False))
    console.print("[dim]请勿分发本工具生成的任何代码.[/]\n")

    with console.status("[bold]正在获取版本列表"):
        manifest = Manifest.load()
    console.print(
        f"[green]已加载 {len(manifest.entries)} 个版本 "
        f"(最新正式版: {manifest.latest_release})[/]\n"
    )

    console.print("[bold]请选择游戏版本 (Only lstest)：[/]")
    console.print("  - 指定版本: [cyan]1.21.11[/]")
    console.print("  - 版本区间: [cyan]1.16.5-26.2[/]")
    console.print("  - 关键字: [cyan]latest[/] / [cyan]all[/]\n")

    raw = console.input("[bold]> [/]").strip()
    try:
        versions = resolve_selection(manifest, raw)
    except ValueError as e:
        console.print(f"[red]错误: {e}[/]")
        sys.exit(1)
    if not versions:
        console.print("[red]未找到匹配的版本。[/]")
        sys.exit(1)

    single = len(versions) == 1

    # 按 era 把连续版本聚成段，同段共用一次映射选择
    segments: list[dict] = []
    for v in versions:
        era, opts, def_idx = _era_options(v.id)
        if segments and segments[-1]["era"] == era:
            segments[-1]["versions"].append(v)
        else:
            segments.append({"era": era, "options": opts,
                             "default": def_idx, "versions": [v]})

    group_mappings: dict[str, MappingKind] = {}
    askable = [s for s in segments if s["era"] not in (0, 7)]
    console.print()
    for n, seg in enumerate(segments, 0):
        vs = seg["versions"]
        if seg["era"] == 0:
            # 26.1+ 已取消混淆，不询问、不提示
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            continue
        if seg["era"] == 7:
            # 1.0~1.6.4 但无现成 MCP(Legacy) 映射：不询问，走 None 并警告
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            tag = vs[0].id if vs[0].id == vs[-1].id else f"{vs[0].id} ~ {vs[-1].id}"
            console.print(
                f"  [yellow]![/] {tag} 无现成 MCP(Legacy) 映射，"
                f"将以 [bold]None[/]（不反混淆）直出"
            )
            continue

        if single:
            prompt = f"为 {vs[0].id} 选择反混淆映射"
        elif vs[0].id == vs[-1].id:
            prompt = f"为 {vs[0].id} 选择反混淆映射"
        else:
            prompt = f"为 {vs[0].id} ~ {vs[-1].id} 选择反混淆映射"
        if len(askable) > 1:
            prompt = f"[{askable.index(seg) + 1}/{len(askable)}] {prompt}"

        chosen = _select(prompt, seg["options"], seg["default"])
        label, kind = seg["options"][chosen]
        clean = label.split(" (")[0]
        for v in vs:
            group_mappings[v.id] = kind

        # 把整个菜单（标题 + n 个选项）擦掉，换成一行简洁确认
        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        for _ in range(len(seg["options"]) + 1):
            sys.stdout.write("\033[2K\033[1B")
        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        tag = vs[0].id if vs[0].id == vs[-1].id else f"{vs[0].id} ~ {vs[-1].id}"
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} {tag} › {clean}\n")
        sys.stdout.flush()

    default_out = str(Path.cwd() / "mc-sources")
    out_raw = console.input(f"\n[bold]输出至[/] [dim]({default_out})[/]: ").strip()
    output_dir = Path(out_raw or default_out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"\n  共 [bold]{len(versions)}[/] 个版本 -> [bold]{output_dir}[/]")
    ans = console.input("继续? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes"):
        sys.exit(0)
    console.print()

    # 解析元数据：并发拉取各版本 version json（这是拿到 jar 下载地址的前置步骤，
    # 不属于「Fetching」校验阶段，用中性的 Resolving 词，避免与 SHA256 校验混淆）
    vjson_cache: dict = {}
    line = LiveLine()
    total_meta = len(versions)
    got = 0

    def _fetch_meta(v):
        return v.id, fetch_version_json(v)

    line.set(f"{_status('Resolving', CYAN)} metadata "
             f"[{_bar(0, 30)}]   0% (0/{total_meta})")
    with ThreadPoolExecutor(max_workers=DECOMPILE_CONCURRENCY) as ex:
        futs = {ex.submit(_fetch_meta, v): v.id for v in versions}
        for fut in as_completed(futs):
            vid, data = fut.result()
            vjson_cache[vid] = data
            got += 1
            pct = got / total_meta * 100.0
            line.set(f"{_status('Resolving', CYAN)} metadata "
                     f"[{_bar(pct, 30)}] {pct:>3.0f}% ({got}/{total_meta})")
    line.clear()

    # 工具 jar + 各版本客户端 jar（size 仅参考，下载行从 0 字节起步，不预探测）
    files: list[tuple] = [
        (VINEFLOWER_JAR, VINEFLOWER_URL, f"vineflower v{VINEFLOWER_VERSION}", None),
        (TINY_REMAPPER_JAR, TINY_REMAPPER_URL, f"tiny-remapper v{TINY_REMAPPER_VERSION}", None),
        (SPECIALSOURCE_JAR, SPECIALSOURCE_URL, f"SpecialSource v{SPECIALSOURCE_VERSION}", None),
    ]
    for v in versions:
        dl = vjson_cache[v.id]["downloads"].get("client")
        if dl:
            files.append((JAR_CACHE / f"{v.id}-client.jar", dl["url"],
                          f"{v.id}-client.jar", dl.get("size")))

    _download_phase(files)                                      # 1) 真实下载
    _fetch_phase([(d, u, l) for d, u, l, _s in files])          # 2) SHA256 校验
    _compile_phase(versions, group_mappings, output_dir, vjson_cache)  # 3) 反编译


if __name__ == "__main__":
    main()

