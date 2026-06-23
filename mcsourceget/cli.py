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
    prepare_mcp_legacy, prepare_legacy_yarn, has_mcp, has_mcp_legacy,
    has_legacy_yarn, MappingArtifacts, resolve_mapping_downloads
)
from .pipeline import process_version
from .versions import resolve_selection, _parse_mc_version
from .config import (
    JAR_CACHE, VINEFLOWER_JAR, VINEFLOWER_URL, VINEFLOWER_VERSION,
    TINY_REMAPPER_JAR, TINY_REMAPPER_URL, TINY_REMAPPER_VERSION,
    SPECIALSOURCE_JAR, SPECIALSOURCE_URL, SPECIALSOURCE_VERSION,
    HTTP_HEADERS, HTTP_TIMEOUT, DECOMPILE_CONCURRENCY, SESSION,
    DOWNLOAD_CONCURRENCY, DOWNLOAD_PER_HOST, DOWNLOAD_CHUNK,
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

    era==0：静默 NONE（26.1+）；era==7：该版本无任何现成映射，警告后走 NONE。
    其余 era 为「按选项种类拼出的元组键」，仅用于把选项完全相同的连续版本归为一段、
    一次性询问。Legacy Yarn 全量铺开：1.3~1.13 凡 Legacy Fabric 有 yarn 的都给出，
    且作为默认（可读 + 含参数名 + 跨版本一致）。
    """
    none_opt = ("None (直出)", MappingKind.NONE)
    yarn_opt = ("Yarn Mappings", MappingKind.YARN)
    lyarn_opt = ("Legacy Yarn", MappingKind.LEGACY_YARN)
    mojang_opt = ("Mojang Mappings", MappingKind.MOJANG)
    mcp_opt = ("MCP Mappings", MappingKind.MCP)
    mcpl_opt = ("MCP Legacy", MappingKind.MCP_LEGACY)
    reborn_opt = ("MCP-Reborn Mappings", MappingKind.MCP_REBORN)

    def ask(options, default_idx):
        # era 用选项种类元组作分组键：选项集相同的相邻版本归并、只问一次
        return tuple(k.value for _, k in options), options, default_idx

    def _default_legacy_yarn(options):
        # 优先默认 Legacy Yarn，没有则默认第 1 个非 None 项，再不行就 None
        for i, (_, k) in enumerate(options):
            if k == MappingKind.LEGACY_YARN:
                return i
        return 1 if len(options) > 1 else 0

    nums = _parse_mc_version(vid)
    if not nums:
        if vid.startswith("2"):
            return 0, [none_opt], 0
        return ask([yarn_opt, mojang_opt, none_opt, reborn_opt], 0)

    if (1, 0) <= nums <= (1, 6, 4):
        opts = [none_opt]
        if has_mcp_legacy(vid):
            opts.append(mcpl_opt)
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        if len(opts) == 1:
            return 7, [none_opt], 0
        return ask(opts, _default_legacy_yarn(opts))
    if (1, 7) <= nums <= (1, 12, 2):
        # MCP 仅对 Forge maven 真有 SRG 的版本；Legacy Yarn 补全其余缺口
        opts = [none_opt]
        if has_mcp(vid):
            opts.append(mcp_opt)
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        if len(opts) == 1:
            return 7, [none_opt], 0
        return ask(opts, _default_legacy_yarn(opts))
    if nums[:2] == (1, 13):
        opts = [none_opt]
        if has_legacy_yarn(vid):
            opts.append(lyarn_opt)
        opts.append(reborn_opt)
        return ask(opts, _default_legacy_yarn(opts))
    if (1, 14) <= nums <= (1, 21, 4):
        return ask([yarn_opt, mojang_opt, none_opt, reborn_opt], 0)
    if (1, 21, 4) < nums <= (1, 21, 11):
        return ask([yarn_opt, mojang_opt, none_opt], 0)
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
            with SESSION.get(url, timeout=HTTP_TIMEOUT, stream=True) as r:
                r.raise_for_status()
                cl = int(r.headers.get("content-length", 0))
                if cl:
                    with lock:
                        total_bytes += cl
                with open(tmp, "wb") as f:
                    # 块设小（256KB）：慢速/限速时进度也能实时往下走，而非整文件下完才跳
                    for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        f.write(chunk)
                        with lock:
                            done_bytes += len(chunk)
            tmp.replace(dest)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        finally:
            sem.release()
        with lock:
            remaining_files -= 1
        return label

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


def _wait_some(futures: set, timeout: float):
    from concurrent.futures import wait, FIRST_COMPLETED
    done, pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
    return done, pending


def _fetch_phase(files: list[tuple[Path, str, str]]) -> None:
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

    def fmt_line(current: str) -> str:
        pct = done / total * 100.0
        return (f"{_status('DeCompiling', CYAN)} [{_bar(pct, 30)}] "
                f"{done}/{total} ({_fmt_elapsed(time.time() - start)})  {current}")

    line.set(fmt_line("初始化"))
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {
            ex.submit(_process_version, v, group_mappings[v.id], output_dir, vjson_cache, kill_lvt, keep_work): v.id
            for v in versions
        }
        for fut in as_completed(futures):
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
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            continue
        if seg["era"] == 7:
            for v in vs:
                group_mappings[v.id] = MappingKind.NONE
            tag = vs[0].id if vs[0].id == vs[-1].id else f"{vs[0].id} ~ {vs[-1].id}"
            console.print(
                f"  [yellow]![/] {tag} 无现成 MCP 映射（该版本从未发布 SRG/映射），"
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

        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        for _ in range(len(seg["options"]) + 1):
            sys.stdout.write("\033[2K\033[1B")
        sys.stdout.write(f"\033[{len(seg['options']) + 1}A")
        tag = vs[0].id if vs[0].id == vs[-1].id else f"{vs[0].id} ~ {vs[-1].id}"
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
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 反编译并发 › 1 线程 (单版本自动推断)\n")
    elif len(versions) < DECOMPILE_CONCURRENCY:
        custom_concurrency = len(versions)
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 反编译并发 › {custom_concurrency} 线程 (随版本数量自动下调)\n")
    else:
        sys.stdout.write(f"\033[2K{BOLD}? 反编译并发数 (严重影响系统卡顿，务必量力而行){RESET} {DIM}(默认 {DECOMPILE_CONCURRENCY}){RESET}\n> ")
        sys.stdout.flush()
        raw_cc = sys.stdin.readline().strip()
        custom_concurrency = int(raw_cc) if raw_cc.isdigit() else DECOMPILE_CONCURRENCY
        sys.stdout.write(f"\033[2A\033[2K\033[1B\033[2K\033[1A")
        sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 反编译并发 › {custom_concurrency} 线程\n")
        sys.stdout.flush()

    lvt_opts = [("是 (移除混淆表，赋予极高代码可读性)", True), ("否 (保留原始 $$1 等恶心变量)", False)]
    kill_lvt = lvt_opts[_select("是否全局移除局部变量表（--kill-lvt）?", lvt_opts, 0)][1]
    sys.stdout.write(f"\033[{len(lvt_opts) + 1}A" + "\033[2K\033[1B" * (len(lvt_opts) + 1) + f"\033[{len(lvt_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 移除 LVT › {'是' if kill_lvt else '否'}\n")
    sys.stdout.flush()

    work_opts = [("否 (完成后自动清理，节省磁盘)", False), ("是 (保留 .jar 等临时文件，便于调试)", True)]
    keep_work = work_opts[_select("调试选项: 是否保留工作目录（work）以供分析?", work_opts, 0)][1]
    sys.stdout.write(f"\033[{len(work_opts) + 1}A" + "\033[2K\033[1B" * (len(work_opts) + 1) + f"\033[{len(work_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 保留工作目录 › {'是' if keep_work else '否'}\n")
    sys.stdout.flush()

    tag_summary = versions[0].id if single else f"{versions[0].id} ~ {versions[-1].id}"
    confirm_opts = [("确定启动", True), ("取消并退出", False)]
    if not confirm_opts[_select(f"共 {len(versions)} 个版本 ({tag_summary}) -> {output_dir}，确认执行?", confirm_opts, 0)][1]:
        console.print("[yellow]已取消操作。[/]")
        sys.exit(0)
    sys.stdout.write(f"\033[{len(confirm_opts) + 1}A" + "\033[2K\033[1B" * (len(confirm_opts) + 1) + f"\033[{len(confirm_opts) + 1}A")
    sys.stdout.write(f"  {GREEN}{'OK':>2}{RESET} 准备就绪，流水线启动...\n\n")
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

    # IO 下载阶段：已经解绑了 CPU 的限制并发，内部强制走极速下载列队
    _download_phase(files)
    _fetch_phase(files)

    # CPU 反编译阶段：严格遵从用户输入或推断的安全并发数
    _compile_phase(versions, group_mappings, output_dir, vjson_cache, custom_concurrency, kill_lvt, keep_work)


if __name__ == "__main__":
    main()