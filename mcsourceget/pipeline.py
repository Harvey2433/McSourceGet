"""处理流水线：对单个版本执行 反混淆 -> 反编译 -> 输出源码。

网络拉取已完全交给 CLI 的主进度条前置处理，此处均为纯离线操作。
"""

from __future__ import annotations

import csv
import shutil
import zipfile
from pathlib import Path

try:
    import javalang
except ImportError:
    javalang = None

from .config import JAR_CACHE, WORK_DIR, DECOMPILE_MAX_HEAP, DECOMPILE_TIMEOUT
from .manifest import VersionEntry, fetch_version_json
from .mappings import MappingArtifacts, MappingKind
from .tools import (
    ensure_specialsource,
    ensure_tiny_remapper,
    ensure_vineflower,
    run_java,
)


def download_client_jar(entry: VersionEntry) -> Path:
    """验证客户端 jar 是否已由 CLI 统一拉取完成。"""
    dest = JAR_CACHE / f"{entry.id}-client.jar"
    if not (dest.exists() and dest.stat().st_size > 0):
        raise FileNotFoundError(f"主下载队列缺失目标 jar，或下载不完整: {dest}")
    return dest


def _remap_mojang(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    ss = ensure_specialsource()
    out = WORK_DIR / version_id / "remapped-mojang.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    cmd_args = [
        "-jar", str(ss),
        "--in-jar", str(client_jar),
        "--out-jar", str(out),
        "--srg-in", str(arts.mojang_txt),
    ]

    run_java(cmd_args)
    return out


def _remap_mcp(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    ss = ensure_specialsource()
    out = WORK_DIR / version_id / "remapped-mcp.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    cmd_args = [
        "-jar", str(ss),
        "--in-jar", str(client_jar),
        "--out-jar", str(out),
        "--srg-in", str(arts.mcp_srg),
    ]

    run_java(cmd_args)
    return out


def _remap_yarn(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    tr = ensure_tiny_remapper()
    out = WORK_DIR / version_id / "remapped-yarn.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    if not arts.yarn_two_step:
        run_java([
            "-jar", str(tr),
            str(client_jar),
            str(out),
            str(arts.yarn_tiny),
            "official",
            "named",
        ])
        return out

    mid = WORK_DIR / version_id / "remapped-intermediary.jar"
    # 两步可能用到「本版 intermediary + 借邻版 yarn」（如 1.7.9 借 1.7.10），
    # Legacy Fabric 映射含少量重名冲突项，忽略冲突保证整体完成。
    run_java([
        "-jar", str(tr),
        str(client_jar),
        str(mid),
        str(arts.intermediary_tiny),
        "official",
        "intermediary",
        "--ignoreConflicts",
    ])
    run_java([
        "-jar", str(tr),
        str(mid),
        str(out),
        str(arts.yarn_tiny),
        "intermediary",
        "named",
        "--ignoreConflicts",
    ])
    return out


def _remap_mcp_legacy(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    tr = ensure_tiny_remapper()
    out = WORK_DIR / version_id / "remapped-mcp-legacy.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    run_java([
        "-jar", str(tr),
        str(client_jar),
        str(out),
        str(arts.legacy_tiny),
        arts.legacy_src_ns or "official",
        "named",
        "--ignoreConflicts",
    ])
    return out


def _decompile(jar: Path, version_id: str, output_dir: Path, kill_lvt: bool) -> Path:
    vf = ensure_vineflower()
    decomp_out = WORK_DIR / version_id / "decompiled"
    decomp_out.mkdir(parents=True, exist_ok=True)

    # 严格遵照 Vineflower v1.12.0 官方文档的完整参数命名！绝不使用废弃或未注明的简写呐！
    vf_args = [
        "-jar", str(vf),
        "--decompile-generics=1",
        "--ascii-strings=1",
        "--remove-synthetic=1",
        "--pattern-matching=1",
        "--variable-renaming=jad",
    ]

    # 梦梦姐亲自查阅文档确认的绝杀：直接命令 Vineflower 无视混淆表
    if kill_lvt:
        vf_args.append("--use-lvt-names=0")

    vf_args.extend([
        "--indent-string=    ",
        str(jar),
        str(decomp_out),
    ])

    run_java(vf_args, jvm_args=[f"-Xmx{DECOMPILE_MAX_HEAP}"], timeout=DECOMPILE_TIMEOUT)

    dest = output_dir / version_id
    dest.mkdir(parents=True, exist_ok=True)

    decomp_jar = decomp_out / jar.with_suffix(".jar").name
    if not decomp_jar.exists():
        jars = list(decomp_out.glob("*.jar"))
        if jars:
            decomp_jar = jars[0]

    if decomp_jar.exists() and zipfile.is_zipfile(decomp_jar):
        with zipfile.ZipFile(decomp_jar) as zf:
            for info in zf.infolist():
                if info.filename.endswith(".java"):
                    zf.extract(info, dest)
    else:
        for java_file in decomp_out.rglob("*.java"):
            rel = java_file.relative_to(decomp_out)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(java_file, target)

    return dest


def _load_csv_names(csv_dir: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for fname in ("methods.csv", "fields.csv", "params.csv"):
        path = csv_dir / fname
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            key_col = "param" if "param" in (reader.fieldnames or []) else "searge"
            for row in reader:
                src = row.get(key_col)
                dst = row.get("name")
                if src and dst:
                    names[src] = dst
    return names


def _apply_mcp_csv_names(src_dir: Path, csv_dir: Path) -> int:
    if javalang is None:
        raise RuntimeError("请使用 pip install javalang 安装依赖，才能进行 AST 级别的安全映射替换！")

    names = _load_csv_names(csv_dir)
    if not names:
        return 0

    total = 0
    for java_file in src_dir.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        try:
            tokens = list(javalang.tokenizer.tokenize(text))
        except javalang.tokenizer.LexerError:
            continue

        lines = text.split("\n")
        changed = False

        for tok in reversed(tokens):
            if isinstance(tok, javalang.tokenizer.Identifier) and tok.value in names:
                repl = names[tok.value]
                l_idx = tok.position.line - 1
                c_idx = tok.position.column - 1

                current_line = lines[l_idx]
                if current_line[c_idx:c_idx + len(tok.value)] == tok.value:
                    lines[l_idx] = current_line[:c_idx] + repl + current_line[c_idx + len(tok.value):]
                    total += 1
                    changed = True

        if changed:
            java_file.write_text("\n".join(lines), encoding="utf-8")

    return total


def process_version(
    entry: VersionEntry,
    arts: MappingArtifacts,
    output_dir: Path,
    kill_lvt: bool,
    keep_work: bool,
) -> Path:
    if arts.kind == MappingKind.MCP_REBORN:
        raise RuntimeError(
            "MCP-Reborn 需要克隆其 Gradle 工程并执行 gradle setup，本工具尚未接入该路线，此版本已跳过。"
        )

    client_jar = download_client_jar(entry)

    if arts.kind == MappingKind.MOJANG:
        jar_to_decompile = _remap_mojang(client_jar, arts, entry.id)
    elif arts.kind in (MappingKind.YARN, MappingKind.LEGACY_YARN, MappingKind.ORNITHE):
        jar_to_decompile = _remap_yarn(client_jar, arts, entry.id)
    elif arts.kind == MappingKind.MCP:
        jar_to_decompile = _remap_mcp(client_jar, arts, entry.id)
    elif arts.kind == MappingKind.MCP_LEGACY:
        jar_to_decompile = _remap_mcp_legacy(client_jar, arts, entry.id)
    else:
        jar_to_decompile = client_jar

    # 直接将 kill_lvt 参数透传给反编译阶段的 Vineflower
    result = _decompile(jar_to_decompile, entry.id, output_dir, kill_lvt)

    if arts.kind == MappingKind.MCP and arts.mcp_csv_dir:
        _apply_mcp_csv_names(result, arts.mcp_csv_dir)

    work = WORK_DIR / entry.id
    if not keep_work and work.exists():
        shutil.rmtree(work, ignore_errors=True)

    return result