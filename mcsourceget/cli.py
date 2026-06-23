"""交互式 CLI 入口：自实现的 Cargo 风格执行流。

  - 按历史断代给出反混淆映射选项
  - Resolving：处理所有 Version JSON，并解析所有 Mappings 依赖 URL
  - Downloading：将外部工具、Client Jar、Mappings 统一压入连接池，绝对避免离线阻塞
  - Fetching：文件完整性校验
  - Compiling：纯离线版本的并发重映射与反编译
"""

from __future__ import annotations

import os
import sys
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.panel import Panel

from .manifest import Manifest, fetch_version_json, VersionEntry
from .mappings import (
    MappingKind, is_unobfuscated, prepare_mojang, prepare_yarn, prepare_mcp,
    prepare_mcp_legacy, prepare_legacy_yarn, prepare_ornithe, has_mcp, has_mcp_legacy,
    has_legacy_yarn, has_ornithe, warm_coverage_caches, MappingArtifacts, resolve_mapping_downloads
)
from .pipeline import process_version
from .versions import resolve_selection, _parse_mc_version
from .config import (
    JAR_CACHE, VINEFLOWER_JAR, VINEFLOWER_URL, VINEFLOWER_VERSION,
    TINY_REMAPPER_JAR, TINY_REMAPPER_URL, TINY_REMAPPER_VERSION,
    SPECIALSOURCE_JAR, SPECIALSOURCE_URL, SPECIALSOURCE_VERSION,
    HTTP_HEADERS, HTTP_TIMEOUT, DECOMPILE_CONCURRENCY, SESSION,
    DOWNLOAD_CONCURRENCY, DOWNLOAD_PER_HOST, DOWNLOAD_CHUNK, DOWNLOAD_RETRIES,
)

console = Console()

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
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def _status(word: str, color: str = GREEN) -> str:
    return f"{color}{word:>12}{RESET}"


def _bar(pct: float, width: int = 40) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100.0 * width)
    if filled >= width:
        return "=" * width
    return "=" * filled + ">" + " " * (width - filled - 1)


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class LiveLine:
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
    """返回 (era, options, default_idx)。

    era==0：静默 NONE（26.1+）；era==7：无任何现成映射，警告后走 NONE。
    其余 era 为「按选项种类拼出的元组键」，把选项相同的连续版本归为一段、一次询问。
    OrnitheMC（Calamus+Feather，~1.3~1.14.4）全量铺开：凡 Ornithe 覆盖的版本都加该选项。
    默认优先级：Yarn(官方) > Legacy Yarn > OrnitheMC > Mojang > MCP > MCP Legacy > MCP-Reborn > None。
    """
    none_opt = ("None (直出)", MappingKind.NONE)
    yarn_opt = ("Yarn Mappings", MappingKind.YARN)
    lyarn_opt = ("Legacy Yarn", MappingKind.LEGACY_YARN)
    ornithe_opt = ("OrnitheMC Mapping", MappingKind.ORNITHE)
    mojang_opt = ("Mojang Mappings", MappingKind.MOJANG)
    mcp_opt = ("MCP Mappings", MappingKind.MCP)
    mcpl_opt = ("MCP Legacy", MappingKind.MCP_LEGACY)
    reborn_opt = ("MCP-Reborn Mappings", MappingKind.MCP_REBORN)

    _priority = [MappingKind.YARN, MappingKind.LEGACY_YARN, MappingKind.ORNITHE,
                 MappingKind.MOJANG, MappingKind.MCP, MappingKind.MCP_LEGACY,
                 MappingKind.MCP_REBORN, MappingKind.NONE]

    def ask(options):
        kinds = [k for _, k in options]
        default = 0
        for p in _priority:
            if p in kinds:
                default = kinds.index(p)
                break
        return tuple(k.value for k in kinds), options, default

    nums = _parse_mc_version(vid)
    if not nums:
        if vid.startswith("2"):
            return 0, [none_opt], 0
        opts = [yarn_opt, mojang_opt, none_opt, reborn_opt]
        if has_ornithe(vid):
            opts.append(ornithe_opt)
        return ask(opts)

    if (1, 0) <= nums <= (1, 6, 4):
        opts = [none_opt]
        if has_mcp_legacy(vid):
            opts.append(mcpl_opt)
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        if has_ornithe(vid):
            opts.append(ornithe_opt)
        return ask(opts) if len(opts) > 1 else (7, [none_opt], 0)
    if (1, 7) <= nums <= (1, 12, 2):
        opts = [none_opt]
        if has_mcp(vid):
            opts.append(mcp_opt)
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        if has_ornithe(vid):
            opts.append(ornithe_opt)
        return ask(opts) if len(opts) > 1 else (7, [none_opt], 0)
    if nums[:2] == (1, 13):
        opts = [none_opt]
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        if has_ornithe(vid):
            opts.append(ornithe_opt)
        opts.append(reborn_opt)
        return ask(opts)
    if (1, 14) <= nums <= (1, 21, 4):
        opts = [yarn_opt, mojang_opt, none_opt, reborn_opt]
        if nums <= (1, 14, 4) and has_ornithe(vid):   # Ornithe 仅覆盖到 1.14.4
            opts.append(ornithe_opt)
        return ask(opts)
    if (1, 21, 4) < nums <= (1, 21, 11):
        return ask([yarn_opt, mojang_opt, none_opt])
    return 0, [none_opt], 0


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl-c"
        return ch
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
    idx = default_idx
    n = len(options)

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\033[{n}A")
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


def _download_phase(files: list[tuple[Path, str, str]]) -> None:
    missing = [f for f in files if not (f[0].exists() and f[0].stat().st_size > 0)]
    if not missing:
        # 全部命中缓存：也留一条可见的总结行，避免看起来像「下载阶段凭空消失」
        sys.stdout.write(f"{_status('Downloaded', GREEN)} {len(files)} cache hits\n")
        sys.stdout.flush()
        return

    # 全局并发可大（多主机并行更快）；同一主机的并发用信号量收着，避免触发 per-IP 限速
    download_concurrency = min(len(missing), DOWNLOAD_CONCURRENCY)

    total_bytes = 0
    done_bytes = 0
    remaining_files = len(missing)
    lock = threading.Lock()
    line = LiveLine()

    # 每个主机一把信号量：同一 host 同时最多 DOWNLOAD_PER_HOST 条流
    host_sems: dict[str, threading.Semaphore] = {}
    host_lock = threading.Lock()

    def _host_sem(url: str) -> threading.Semaphore:
        host = urlparse(url).netloc
        with host_lock:
            sem = host_sems.get(host)
            if sem is None:
                sem = threading.Semaphore(DOWNLOAD_PER_HOST)
                host_sems[host] = sem
            return sem

    def fmt_line() -> str:
        remaining = max(0, total_bytes - done_bytes)
        return (f"{_status('Downloading', CYAN)} {remaining_files} crates, "
                f"remaining bytes: {_human_bytes(remaining)}")

    line.set(fmt_line())

    def worker(dest: Path, url: str, label: str) -> str:
        nonlocal total_bytes, done_bytes, remaining_files
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        sem = _host_sem(url)
        sem.acquire()
        try:
            last_err = None
            for attempt in range(DOWNLOAD_RETRIES):
                my_total = 0          # 本次尝试计入 total 的量（失败时回滚）
                my_done = 0           # 本次尝试计入 done 的量
                try:
                    with SESSION.get(url, timeout=HTTP_TIMEOUT, stream=True) as r:
                        r.raise_for_status()
                        cl = int(r.headers.get("content-length", 0))
                        if cl:
                            my_total = cl
                            with lock:
                                total_bytes += cl
                        with open(tmp, "wb") as f:
                            # 块设小（256KB）：慢速/限速时进度也能实时往下走，而非整文件下完才跳
                            for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                                f.write(chunk)
                                my_done += len(chunk)
                                with lock:
                                    done_bytes += len(chunk)
                    tmp.replace(dest)
                    with lock:
                        remaining_files -= 1
                    return label
                except Exception as e:
                    last_err = e
                    # 回滚本次已计入的字节/总量，清掉半成品，准备重试
                    with lock:
                        total_bytes -= my_total
                        done_bytes -= my_done
                    if tmp.exists():
                        tmp.unlink()
                    if attempt < DOWNLOAD_RETRIES - 1:
                        time.sleep(1.0 * (attempt + 1))     # 退避 1s / 2s
            raise last_err
        finally:
            sem.release()

    with ThreadPoolExecutor(max_workers=download_concurrency) as ex:
        futs = {ex.submit(worker, d, u, l): l for d, u, l in missing}
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
    sys.stdout.flush()


def _wait_some(futures: set, timeout: float):
    from concurrent.futures import wait, FIRST_COMPLETED
    done, pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
    return done, pending


def _fetch_phase(files: list[tuple[Path, str, str]]) -> None:
    line = LiveLine()
    checked = 0
    for dest, _url, label in files:
        if not dest.exists():
            continue
        checked += 1
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
    sys.stdout.write(f"{_status('Fetching', GREEN)} Verification complete.\n")
    sys.stdout.flush()


def _process_version(entry: VersionEntry, kind: MappingKind,
                     output_dir: Path, vjson_cache: dict, kill_lvt: bool, keep_work: bool) -> tuple[bool, str]:
    try:
        vjson = vjson_cache[entry.id]
        actual = kind
        if is_unobfuscated(entry, vjson):
            actual = MappingKind.NONE
        if actual == MappingKind.MOJANG:
            arts = prepare_mojang(entry, vjson)
        elif actual == MappingKind.YARN:
            arts = prepare_yarn(entry, entry.id)
        elif actual == MappingKind.LEGACY_YARN:
            arts = prepare_legacy_yarn(entry, entry.id)
        elif actual == MappingKind.ORNITHE:
            arts = prepare_ornithe(entry, entry.id)
        elif actual == MappingKind.MCP:
            arts = prepare_mcp(entry, entry.id)
        elif actual == MappingKind.MCP_LEGACY:
            arts = prepare_mcp_legacy(entry, entry.id)
        elif actual == MappingKind.MCP_REBORN:
            arts = MappingArtifacts(kind=MappingKind.MCP_REBORN)
        else:
            arts = MappingArtifacts(kind=MappingKind.NONE)
        result = process_version(entry, arts, output_dir, kill_lvt, keep_work)
        return (True, str(result))
    except Exception as e:
        return (False, str(e))


def _compile_phase(versions, group_mappings, output_dir, vjson_cache, concurrency, kill_lvt, keep_work) -> None:
    line = LiveLine()
    total = len(versions)
    done = 0
    start = time.time()
    in_progress: list[str] = []          # 真正在跑的版本（worker 进入时加入、退出时移除）
    lock = threading.Lock()

    def fmt_line() -> str:
        pct = done / total * 100.0
        with lock:
            running = list(in_progress)
        if running:
            cur = "、".join(running[:3]) + (f" 等{len(running)}个" if len(running) > 3 else "")
        else:
            cur = "完成" if done >= total else "等待空闲线程"
        return (f"{_status('Processing', CYAN)} [{_bar(pct, 30)}] "
                f"{done}/{total} ({_fmt_elapsed(time.time() - start)})  {cur}")

    def worker(v):
        with lock:
            in_progress.append(v.id)
        try:
            return _process_version(v, group_mappings[v.id], output_dir, vjson_cache, kill_lvt, keep_work)
        finally:
            with lock:
                if v.id in in_progress:
                    in_progress.remove(v.id)

    line.set(fmt_line())
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(worker, v): v.id for v in versions}
        pending = set(futures)
        while pending:
            # 每 0.2s 轮询一次：即便单个版本反编译很久，进度行的计时与「正在处理」也会实时跳动
            done_set, pending = _wait_some(pending, timeout=0.2)
            for fut in done_set:
                vid = futures[fut]
                try:
                    ok, msg = fut.result()
                    if ok:
                        line.log(f"{_status('Finished', GREEN)} {vid}")
                    else:
                        line.log(f"{_status('Failed', RED)} {vid} -> {msg}")
                except Exception as e:
                    line.log(f"{_status('Error', RED)} {vid} -> {e}")
                done += 1
            line.set(fmt_line())
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

    # 预热历史映射覆盖表：只要选区涉及 ≤1.14.4（或老式快照），就提前并发拉好
    # Legacy Fabric / Ornithe 的版本清单，避免随后静默构建选项时逐项联网像卡死。
    def _needs_cov(v: VersionEntry) -> bool:
        n = _parse_mc_version(v.id)
        return (n <= (1, 14, 4)) if n else (not v.id.startswith("2"))
    if any(_needs_cov(v) for v in versions):
        with console.status("[bold]正在加载历史版本映射表"):
            warm_coverage_caches()

    segments: list[dict] = []
    for v in versions:
        era, opts, def_idx = _era_options(v.id)
        if segments and segments[-1]["era"] == era:
            segments[-1]["versions"].append(v)
        else:
            segments.append({"era": era, "options": opts,
                             "default": def_idx, "versions": [v]})

    group_mappings: dict[str, MappingKind] = {}
    answered: dict[tuple, MappingKind] = {}   # 按「选项列表」缓存：同样的列表只问一次
    console.print()
    for seg in segments:
        vs = seg["versions"]
        tag = vs[0].id if vs[0].id == vs[-1].id else f"{vs[0].id} ~ {vs[-1].id}"

        if seg["era"] == 0:
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            continue
        if seg["era"] == 7:
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            console.print(
                f"  [yellow]![/] {tag} 无现成映射（从未发布 SRG/映射），"
                f"将以 [bold]None[/]（不反混淆）直出"
            )
            continue

        sig = tuple(k for _, k in seg["options"])

        # 完全相同的选项列表此前已选过 → 按那次选择自动沿用，不再问
        if sig in answered:
            kind = answered[sig]
            for v in vs:
                group_mappings[v.id] = kind
            clean = next(lbl.split(" (")[0] for lbl, k in seg["options"] if k == kind)
            console.print(f"  [dim]↺ {tag} › {clean}（复用）[/]")
            continue

        # 新的选项列表：问一次，并记住
        chosen = _select(f"为 {tag} 选择反混淆映射", seg["options"], seg["default"])
        label, kind = seg["options"][chosen]
        clean = label.split(" (")[0]
        for v in vs:
            group_mappings[v.id] = kind
        answered[sig] = kind

        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        for _ in range(len(seg["options"]) + 1):
            sys.stdout.write("\033[2K\033[1B")
        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} {tag} › {clean}\n")
        sys.stdout.flush()

    # ---- 交互参数收集区域（极致强迫症终端清理，绝无原生 input 踩踏） ----
    console.print()
    default_out = str(Path.cwd() / "mc-sources")
    sys.stdout.write(f"\033[2K{BOLD}? 输出至{RESET} {DIM}({default_out}){RESET}\n> ")
    sys.stdout.flush()
    out_raw = sys.stdin.readline().strip()
    output_dir = Path(out_raw or default_out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # 优雅擦除手打的文本留存行
    sys.stdout.write(f"\033[2A\033[2K\033[1B\033[2K\033[1A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 输出目录 › {output_dir}\n")
    sys.stdout.flush()

    # 梦梦姐的防卡死智能并发判断
    if len(versions) <= 1:
        custom_concurrency = 1
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 并发数量 › 1 线程\n")
    elif len(versions) < DECOMPILE_CONCURRENCY:
        custom_concurrency = len(versions)
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 并发数量 › {custom_concurrency} 线程\n")
    else:
        sys.stdout.write(f"\033[2K{BOLD}? 并发数量 (严重影响系统卡顿，务必量力而行){RESET} {DIM}(默认 {DECOMPILE_CONCURRENCY}){RESET}\n> ")
        sys.stdout.flush()
        raw_cc = sys.stdin.readline().strip()
        custom_concurrency = int(raw_cc) if raw_cc.isdigit() else DECOMPILE_CONCURRENCY
        sys.stdout.write(f"\033[2A\033[2K\033[1B\033[2K\033[1A")
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 并发数量 › {custom_concurrency} 线程\n")
        sys.stdout.flush()

    lvt_opts = [("Yes", True), ("No", False)]
    kill_lvt = lvt_opts[_select("是否全局移除局部变量表?", lvt_opts, 0)][1]
    sys.stdout.write(f"\033[{len(lvt_opts) + 1}A" + "\033[2K\033[1B" * (len(lvt_opts) + 1) + f"\033[{len(lvt_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 移除 LVT › {'Yes' if kill_lvt else 'No'}\n")
    sys.stdout.flush()

    work_opts = [("No", False), ("Yes", True)]
    keep_work = work_opts[_select("是否保留工作目录?", work_opts, 0)][1]
    sys.stdout.write(f"\033[{len(work_opts) + 1}A" + "\033[2K\033[1B" * (len(work_opts) + 1) + f"\033[{len(work_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 保留工作目录 › {'Yes' if keep_work else 'No'}\n")
    sys.stdout.flush()

    tag_summary = versions[0].id if single else f"{versions[0].id} ~ {versions[-1].id}"
    confirm_opts = [("Yes", True), ("No", False)]
    if not confirm_opts[_select(f"总计 {len(versions)} 个版本 ({tag_summary}) -> {output_dir}，确认?", confirm_opts, 0)][1]:
        console.print("[yellow]操作已被取消.[/]")
        sys.exit(0)
    sys.stdout.write(f"\033[{len(confirm_opts) + 1}A" + "\033[2K\033[1B" * (len(confirm_opts) + 1) + f"\033[{len(confirm_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} Processing has begun.\n\n")
    sys.stdout.flush()
    # ---- 交互参数收集结束 ----

    files: list[tuple[Path, str, str]] = [
        (VINEFLOWER_JAR, VINEFLOWER_URL, f"vineflower v{VINEFLOWER_VERSION}"),
        (TINY_REMAPPER_JAR, TINY_REMAPPER_URL, f"tiny-remapper v{TINY_REMAPPER_VERSION}"),
        (SPECIALSOURCE_JAR, SPECIALSOURCE_URL, f"SpecialSource v{SPECIALSOURCE_VERSION}"),
    ]

    vjson_cache: dict = {}
    line = LiveLine()
    total_meta = len(versions)
    got = 0

    def _fetch_meta(v):
        vjson = fetch_version_json(v)
        mapping_kind = group_mappings[v.id]

        dls = resolve_mapping_downloads(v, vjson, mapping_kind)
        dl = vjson["downloads"].get("client")
        if dl:
            dls.append((JAR_CACHE / f"{v.id}-client.jar", dl["url"], f"{v.id}-client.jar"))

        return v.id, vjson, dls

    line.set(f"{_status('Resolving', CYAN)} metadata [{_bar(0, 30)}]   0% (0/{total_meta})")

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_meta, v): v.id for v in versions}
        for fut in as_completed(futs):
            vid, data, extracted_downloads = fut.result()
            vjson_cache[vid] = data
            files.extend(extracted_downloads)
            got += 1
            pct = got / total_meta * 100.0
            line.set(f"{_status('Resolving', CYAN)} metadata "
                     f"[{_bar(pct, 30)}] {pct:>3.0f}% ({got}/{total_meta})")
    line.clear()
    sys.stdout.write(f"{_status('Resolving', GREEN)} Analysis complete.\n")
    sys.stdout.flush()
    console.print()

    # IO 下载阶段：多主机并行 + 同主机限流 + 单文件失败重试
    _download_phase(files)
    console.print()
    # 完整性校验
    _fetch_phase(files)
    console.print()

    # CPU 反编译阶段：严格遵从用户输入或推断的安全并发数
    _compile_phase(versions, group_mappings, output_dir, vjson_cache, custom_concurrency, kill_lvt, keep_work)
    console.print(f"\n[bold green]All processes completed in[/] -> [bold]{output_dir}[/]")


if __name__ == "__main__":
    main()