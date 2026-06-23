"""处理流水线：对单个版本执行 下载jar -> 反混淆 -> 反编译 -> 输出源码。

根据 MappingKind 走不同路线：
  MOJANG: client.jar -> SpecialSource(ProGuard反转) -> remapped.jar -> Vineflower -> src/
  YARN:   client.jar -> tiny-remapper(official->named via mergedv2) -> remapped.jar -> Vineflower -> src/
  NONE:   client.jar -> Vineflower -> src/
"""

from __future__ import annotations

import csv
import re
import shutil
import zipfile
from pathlib import Path

from .config import JAR_CACHE, WORK_DIR, DECOMPILE_MAX_HEAP
from .manifest import VersionEntry, fetch_version_json
from .mappings import MappingArtifacts, MappingKind
from .tools import (
    ensure_specialsource,
    ensure_tiny_remapper,
    ensure_vineflower,
    run_java,
)


def download_client_jar(entry: VersionEntry) -> Path:
    vjson = fetch_version_json(entry)
    dl = vjson["downloads"]["client"]
    dest = JAR_CACHE / f"{entry.id}-client.jar"

    # 增加 ZIP 文件完整性校验，避免残缺文件导致 ZipException
    if dest.exists() and dest.stat().st_size > 0:
        if zipfile.is_zipfile(dest):
            return dest
        else:
            dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(".tmp")

    from .config import HTTP_TIMEOUT, SESSION

    resp = SESSION.get(dl["url"], timeout=HTTP_TIMEOUT, stream=True)
    resp.raise_for_status()
    with open(tmp_dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            f.write(chunk)

    # 下载完成后安全重命名
    shutil.move(str(tmp_dest), str(dest))
    return dest


def _remap_mojang(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    ss = ensure_specialsource()
    out = WORK_DIR / version_id / "remapped-mojang.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    run_java([
        "-jar", str(ss),
        "--in-jar", str(client_jar),
        "--out-jar", str(out),
        "--srg-in", str(arts.mojang_txt),
        "--kill-lvt",
    ])
    return out


def _remap_mcp(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    """MCP 路线第一步：用 joined.srg 把 notch 字节码重映射为 searge 名。"""
    ss = ensure_specialsource()
    out = WORK_DIR / version_id / "remapped-mcp.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    run_java([
        "-jar", str(ss),
        "--in-jar", str(client_jar),
        "--out-jar", str(out),
        "--srg-in", str(arts.mcp_srg),
        "--kill-lvt",
    ])
    return out


def _remap_yarn(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    tr = ensure_tiny_remapper()
    out = WORK_DIR / version_id / "remapped-yarn.jar"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    if not arts.yarn_two_step:
        # mergedv2：一步 official -> named（三列 tiny 含 official/intermediary/named）
        run_java([
            "-jar", str(tr),
            str(client_jar),
            str(out),
            str(arts.yarn_tiny),
            "official",
            "named",
        ])
        return out

    # 无 mergedv2（1.14.x/1.15.x）：两步
    #   1) official -> intermediary（用 intermediary.tiny）
    #   2) intermediary -> named（用 yarn v2）
    mid = WORK_DIR / version_id / "remapped-intermediary.jar"
    run_java([
        "-jar", str(tr),
        str(client_jar),
        str(mid),
        str(arts.intermediary_tiny),
        "official",
        "intermediary",
    ])
    run_java([
        "-jar", str(tr),
        str(mid),
        str(out),
        str(arts.yarn_tiny),
        "intermediary",
        "named",
    ])
    return out


def _remap_mcp_legacy(client_jar: Path, arts: MappingArtifacts, version_id: str) -> Path:
    """MCP(Legacy)：RetroMCP 的 mappings.tiny 直接是 混淆名 -> named，
    复用 tiny-remapper，源命名空间随版本不同（official 或 client）。"""
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
        # 老映射存在少量重名冲突项（原为 RetroMCP 自家 remapper 设计），
        # 忽略冲突，保证整体能完成重映射
        "--ignoreConflicts",
    ])
    return out


def _decompile(jar: Path, version_id: str, output_dir: Path) -> Path:
    vf = ensure_vineflower()
    decomp_out = WORK_DIR / version_id / "decompiled"
    decomp_out.mkdir(parents=True, exist_ok=True)

    run_java([
        "-jar", str(vf),
        "-dgs=1",   # decompile generic signatures
        "-asc=1",   # ascii string characters
        "-rsy=1",   # remove synthetic
        "-ind=    ",  # 4-space indent
        str(jar),
        str(decomp_out),
    ], jvm_args=[f"-Xmx{DECOMPILE_MAX_HEAP}"])

    # Vineflower outputs a jar with .java files inside, or a directory.
    # Find the result and copy .java files to output_dir.
    dest = output_dir / version_id
    dest.mkdir(parents=True, exist_ok=True)

    # Check if Vineflower produced a jar or directory of java files
    decomp_jar = decomp_out / jar.with_suffix(".jar").name
    if not decomp_jar.exists():
        # look for any jar in the output
        jars = list(decomp_out.glob("*.jar"))
        if jars:
            decomp_jar = jars[0]

    if decomp_jar.exists() and zipfile.is_zipfile(decomp_jar):
        with zipfile.ZipFile(decomp_jar) as zf:
            for info in zf.infolist():
                if info.filename.endswith(".java"):
                    zf.extract(info, dest)
    else:
        # Vineflower wrote .java files directly
        for java_file in decomp_out.rglob("*.java"):
            rel = java_file.relative_to(decomp_out)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(java_file, target)

    return dest


# searge token：func_<num>_<suffix>（方法）/ field_<num>_<suffix>（字段）/ p_<...>（参数）
_SEARGE_RE = re.compile(r"\b(?:func|field|p)_\w+\b")


def _load_csv_names(csv_dir: Path) -> dict[str, str]:
    """把 fields/methods/params.csv 合并成 searge -> 可读名 的总表。"""
    names: dict[str, str] = {}
    for fname in ("methods.csv", "fields.csv", "params.csv"):
        path = csv_dir / fname
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            # methods/fields 的列是 searge,name,...；params 的列是 param,name,...
            key_col = "param" if "param" in (reader.fieldnames or []) else "searge"
            for row in reader:
                src = row.get(key_col)
                dst = row.get("name")
                if src and dst:
                    names[src] = dst
    return names


def _apply_mcp_csv_names(src_dir: Path, csv_dir: Path) -> int:
    """对反编译出的 .java 做源码级 searge->可读名替换，返回改名命中次数。"""
    names = _load_csv_names(csv_dir)
    if not names:
        return 0

    total = 0

    def _sub(m: re.Match) -> str:
        nonlocal total
        tok = m.group(0)
        repl = names.get(tok)
        if repl is None:
            return tok
        total += 1
        return repl

    for java_file in src_dir.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        new_text = _SEARGE_RE.sub(_sub, text)
        if new_text != text:
            java_file.write_text(new_text, encoding="utf-8")
    return total


def process_version(
    entry: VersionEntry,
    arts: MappingArtifacts,
    output_dir: Path,
) -> Path:
    """完整处理一个版本：下载 -> 反混淆 -> 反编译 -> 输出。返回输出目录。"""
    if arts.kind == MappingKind.MCP_REBORN:
        raise RuntimeError(
            "MCP-Reborn 需要克隆其 Gradle 工程并执行 gradle setup（旧分支还需 "
            "JDK 16/17），本工具尚未接入该路线，此版本已跳过。"
        )

    client_jar = download_client_jar(entry)

    if arts.kind == MappingKind.MOJANG:
        jar_to_decompile = _remap_mojang(client_jar, arts, entry.id)
    elif arts.kind == MappingKind.YARN:
        jar_to_decompile = _remap_yarn(client_jar, arts, entry.id)
    elif arts.kind == MappingKind.MCP:
        jar_to_decompile = _remap_mcp(client_jar, arts, entry.id)
    elif arts.kind == MappingKind.MCP_LEGACY:
        jar_to_decompile = _remap_mcp_legacy(client_jar, arts, entry.id)
    else:
        jar_to_decompile = client_jar

    result = _decompile(jar_to_decompile, entry.id, output_dir)

    # MCP：反编译后再做源码级 searge->可读名替换
    if arts.kind == MappingKind.MCP and arts.mcp_csv_dir:
        _apply_mcp_csv_names(result, arts.mcp_csv_dir)

    # 清理工作目录节省空间
    work = WORK_DIR / entry.id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)

    return result