"""Mappings fetch and preparation.

Supports three deobfuscation strategies:
  MOJANG -- official ProGuard mapping (19w36a ~ 1.21.11).
  YARN   -- community mapping with friendly names + param names.
  NONE   -- unobfuscated versions (26.1+), no mapping needed.
"""

from __future__ import annotations

import enum
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .config import (
    FABRIC_MAVEN, FABRIC_META, HTTP_HEADERS, HTTP_TIMEOUT, MAP_CACHE,
    MCP_SRG_URL, MCP_SRG_VERSIONS, MCP_CSV_CHANNELS, MCP_CSV_METADATA, MCP_CSV_URL,
    MCP_LEGACY_MAPPINGS, LEGACY_FABRIC_META, LEGACY_FABRIC_MAVEN,
    ORNITHE_META, ORNITHE_MAVEN, SESSION,
)
from .manifest import VersionEntry


class MappingKind(enum.Enum):
    MOJANG = "mojang"
    YARN = "yarn"
    LEGACY_YARN = "legacy_yarn"
    ORNITHE = "ornithe"
    MCP = "mcp"
    MCP_LEGACY = "mcp_legacy"
    MCP_REBORN = "mcp_reborn"
    NONE = "none"

    @property
    def label(self) -> str:
        _map = {
            MappingKind.MOJANG: "Mojang official mapping",
            MappingKind.YARN: "Yarn (community, friendly names + param names)",
            MappingKind.LEGACY_YARN: "Legacy Yarn (Legacy Fabric, 1.3~1.13)",
            MappingKind.ORNITHE: "OrnitheMC Mapping (Calamus + Feather, ~1.3~1.14.4)",
            MappingKind.MCP: "MCP (classic SRG + CSV names)",
            MappingKind.MCP_LEGACY: "MCP Legacy (RetroMCP bundled tiny mapping)",
            MappingKind.MCP_REBORN: "MCP-Reborn (Gradle project)",
            MappingKind.NONE: "None (version is unobfuscated)",
        }
        return _map[self]


@dataclass
class MappingArtifacts:
    """Resolved mapping file paths for a single version."""

    kind: MappingKind
    mojang_txt: Optional[Path] = None
    intermediary_tiny: Optional[Path] = None
    yarn_tiny: Optional[Path] = None
    mcp_srg: Optional[Path] = None
    mcp_csv_dir: Optional[Path] = None
    legacy_tiny: Optional[Path] = None
    legacy_src_ns: Optional[str] = None
    yarn_two_step: bool = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get_json(url: str):
    resp = SESSION.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _download_bytes(url: str) -> bytes:
    resp = SESSION.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# unobfuscated detection
# ---------------------------------------------------------------------------
def _is_modern(entry: VersionEntry) -> bool:
    cutoff = datetime(2025, 11, 1, tzinfo=timezone.utc)
    rt = entry.release_time
    if rt.tzinfo is None:
        rt = rt.replace(tzinfo=timezone.utc)
    return rt >= cutoff


def is_unobfuscated(entry: VersionEntry, vjson: dict) -> bool:
    if "unobfuscated" in entry.id.lower():
        return True
    downloads = vjson.get("downloads", {})
    has_client = "client" in downloads
    has_map = "client_mappings" in downloads
    return has_client and not has_map and _is_modern(entry)


# ---------------------------------------------------------------------------
# Pre-resolution of Downloads (For unified CLI downloading)
# ---------------------------------------------------------------------------
def _resolve_yarn_like(entry: VersionEntry, meta_base: str, maven_base: str,
                       yarn_group: str) -> list[tuple[Path, str, str]]:
    """官方 Yarn 与 Legacy Yarn 共用：解析 intermediary + yarn 的下载列表。

    两者接口同构：meta v2 给出 intermediary/yarn 的 maven 坐标，maven 上各有
    v2 / mergedv2 classifier。优先 mergedv2（一步 official->named），缺则用 v2
    （两步 official->intermediary->named）。缓存文件名与 prepare_yarn 一致。
    """
    downloads: list[tuple[Path, str, str]] = []
    gv = entry.id
    try:
        inter_list = _get_json(f"{meta_base}/versions/intermediary/{gv}")
    except requests.HTTPError:
        inter_list = []
    if inter_list:
        inter_url = _maven_to_url(inter_list[0]["maven"], classifier="v2",
                                  maven_base=maven_base)
        downloads.append((MAP_CACHE / entry.id / "intermediary-v2.jar",
                          inter_url, f"{entry.id}-intermediary.jar"))

    try:
        builds = _get_json(f"{meta_base}/versions/yarn/{gv}")
    except requests.HTTPError:
        builds = []
    if builds:
        stable = [b for b in builds if b.get("stable")]
        yarn_ver = (stable or builds)[0]["version"]
        coord = f"{yarn_group}:yarn:{yarn_ver}"
        merged_url = _maven_to_url(coord, classifier="mergedv2", maven_base=maven_base)
        try:
            resp = SESSION.head(merged_url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            downloads.append((MAP_CACHE / entry.id / "yarn-mergedv2.jar",
                              merged_url, f"{entry.id}-yarn-mergedv2.jar"))
        except requests.HTTPError:
            v2_url = _maven_to_url(coord, classifier="v2", maven_base=maven_base)
            downloads.append((MAP_CACHE / entry.id / "yarn-v2.jar",
                              v2_url, f"{entry.id}-yarn-v2.jar"))
    return downloads


def _resolve_legacy_yarn(entry: VersionEntry) -> list[tuple[Path, str, str]]:
    """Legacy Yarn 下载解析：本版直出或借邻版 yarn。

    intermediary 始终用本版自己的（official 列对得上本版 jar）；yarn 层可借邻版，
    借用时只取 intermediary->named，故强制存为 yarn-v2.jar 触发两步重映射。
    """
    plan = legacy_yarn_plan(entry.id)
    if plan is None:
        return []
    inter_ver, yarn_ver, borrow = plan
    downloads: list[tuple[Path, str, str]] = []

    inter_list = _get_json(f"{LEGACY_FABRIC_META}/versions/intermediary/{inter_ver}")
    if inter_list:
        inter_url = _maven_to_url(inter_list[0]["maven"], classifier="v2",
                                  maven_base=LEGACY_FABRIC_MAVEN)
        downloads.append((MAP_CACHE / entry.id / "intermediary-v2.jar",
                          inter_url, f"{entry.id}-intermediary.jar"))

    builds = _get_json(f"{LEGACY_FABRIC_META}/versions/yarn/{yarn_ver}")
    if builds:
        yv = ([b for b in builds if b.get("stable")] or builds)[0]["version"]
        coord = f"net.legacyfabric:yarn:{yv}"
        merged_url = _maven_to_url(coord, classifier="mergedv2", maven_base=LEGACY_FABRIC_MAVEN)
        try:
            resp = SESSION.head(merged_url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            yarn_url, has_merged = merged_url, True
        except requests.HTTPError:
            yarn_url = _maven_to_url(coord, classifier="v2", maven_base=LEGACY_FABRIC_MAVEN)
            has_merged = False
        if borrow:
            # 借用：必须走 intermediary->named 两步 -> 存为 v2 文件名（即便内容是 mergedv2）
            label = f"{entry.id}-yarn(借 {yarn_ver})"
            downloads.append((MAP_CACHE / entry.id / "yarn-v2.jar", yarn_url, label))
        elif has_merged:
            downloads.append((MAP_CACHE / entry.id / "yarn-mergedv2.jar",
                              yarn_url, f"{entry.id}-yarn-mergedv2.jar"))
        else:
            downloads.append((MAP_CACHE / entry.id / "yarn-v2.jar",
                              yarn_url, f"{entry.id}-yarn-v2.jar"))
    return downloads


# ---------------------------------------------------------------------------
# OrnitheMC（~1.3~1.14.4）：Calamus(intermediary) + Feather(named)，两步重映射
# ---------------------------------------------------------------------------
_ornithe_feather_versions_cache: Optional[set] = None


def _ornithe_feather_versions() -> set:
    """Ornithe Feather 覆盖的全部 gameVersion，缓存（网络失败回空集）。"""
    global _ornithe_feather_versions_cache
    if _ornithe_feather_versions_cache is None:
        try:
            data = _get_json(f"{ORNITHE_META}/versions/feather")
            _ornithe_feather_versions_cache = {b["gameVersion"] for b in data if b.get("gameVersion")}
        except Exception:
            _ornithe_feather_versions_cache = set()
    return _ornithe_feather_versions_cache


def _ornithe_game_version(version_id: str) -> Optional[str]:
    """把 Mojang 版本 id 映射到 Ornithe 的 gameVersion。

    多数正式版同名直配；少数 Ornithe 带后缀（如 1.6.2 -> 1.6.2-091847，
    1.7.7 -> 1.7.7-101331），用「精确优先、否则唯一前缀」匹配。
    """
    vs = _ornithe_feather_versions()
    if version_id in vs:
        return version_id
    prefixed = [g for g in vs if g.startswith(version_id + "-")]
    return prefixed[0] if len(prefixed) == 1 else None


def has_ornithe(version_id: str) -> bool:
    """该版本是否有 OrnitheMC（Feather 可读名）映射。"""
    return _ornithe_game_version(version_id) is not None


def _resolve_ornithe(entry: VersionEntry) -> list[tuple[Path, str, str]]:
    """Ornithe 下载解析：calamus(official->intermediary) + feather(intermediary->named)。

    两者都取 v2 classifier；feather 存为 yarn-v2.jar 触发两步重映射。
    """
    gv = _ornithe_game_version(entry.id)
    if gv is None:
        return []
    downloads: list[tuple[Path, str, str]] = []

    cal = _get_json(f"{ORNITHE_META}/versions/intermediary/{gv}")
    if cal:
        cal_url = _maven_to_url(cal[0]["maven"], classifier="v2", maven_base=ORNITHE_MAVEN)
        downloads.append((MAP_CACHE / entry.id / "intermediary-v2.jar",
                          cal_url, f"{entry.id}-calamus.jar"))

    feath = _get_json(f"{ORNITHE_META}/versions/feather/{gv}")
    if feath:
        fv = ([b for b in feath if b.get("stable")] or feath)[0]["version"]
        feath_url = _maven_to_url(f"net.ornithemc:feather:{fv}", classifier="v2",
                                  maven_base=ORNITHE_MAVEN)
        downloads.append((MAP_CACHE / entry.id / "yarn-v2.jar",
                          feath_url, f"{entry.id}-feather.jar"))
    return downloads


def prepare_ornithe(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    """与 prepare_yarn 读取同样的缓存文件（calamus 存为 intermediary、feather 存为 yarn-v2），
    走两步 official->intermediary->named，仅把 kind 标为 ORNITHE。"""
    arts = prepare_yarn(entry, game_version)
    arts.kind = MappingKind.ORNITHE
    return arts


def resolve_mapping_downloads(entry: VersionEntry, vjson: dict, kind: MappingKind) -> list[tuple[Path, str, str]]:
    """将所有需要的 Mapping 文件 URL 提前吐出，供 CLI 主进度条统一并发下载。"""
    downloads = []
    if kind == MappingKind.MOJANG:
        dl = vjson.get("downloads", {}).get("client_mappings")
        if dl:
            downloads.append((MAP_CACHE / entry.id / "client_mappings.txt", dl["url"], f"{entry.id}-mojang.txt"))
    elif kind == MappingKind.YARN:
        downloads += _resolve_yarn_like(
            entry, FABRIC_META, FABRIC_MAVEN, "net.fabricmc")
    elif kind == MappingKind.LEGACY_YARN:
        downloads += _resolve_legacy_yarn(entry)
    elif kind == MappingKind.MCP:
        game_version = entry.id
        srg_url = MCP_SRG_URL.format(ver=game_version)
        downloads.append((MAP_CACHE / entry.id / "mcp-srg.zip", srg_url, f"{entry.id}-srg.zip"))

        pick = _best_csv(game_version)
        if pick:
            channel, cver = pick
            csv_url = MCP_CSV_URL.format(channel=channel, cver=cver)
            downloads.append((MAP_CACHE / entry.id / "mcp-csv.zip", csv_url, f"{entry.id}-csv.zip"))
    elif kind == MappingKind.MCP_LEGACY:
        url = MCP_LEGACY_MAPPINGS.get(entry.id)
        if url:
            downloads.append((MAP_CACHE / entry.id / "legacy-mappings.zip", url, f"{entry.id}-legacy.zip"))
    elif kind == MappingKind.ORNITHE:
        downloads += _resolve_ornithe(entry)
    return downloads


# ---------------------------------------------------------------------------
# Mojang official mapping
# ---------------------------------------------------------------------------
def prepare_mojang(entry: VersionEntry, vjson: dict) -> MappingArtifacts:
    dest = MAP_CACHE / entry.id / "client_mappings.txt"
    return MappingArtifacts(kind=MappingKind.MOJANG, mojang_txt=dest)


# ---------------------------------------------------------------------------
# Yarn mapping
# ---------------------------------------------------------------------------
def yarn_builds(game_version: str) -> list[dict]:
    return _get_json(f"{FABRIC_META}/versions/yarn/{game_version}")


def best_yarn_version(game_version: str) -> Optional[str]:
    builds = yarn_builds(game_version)
    if not builds:
        return None
    stable = [b for b in builds if b.get("stable")]
    return (stable or builds)[0]["version"]


def has_yarn(game_version: str) -> bool:
    try:
        return bool(yarn_builds(game_version))
    except requests.HTTPError:
        return False


def _maven_to_url(coord: str, *, classifier: str | None = None, ext: str = "jar",
                  maven_base: str = FABRIC_MAVEN) -> str:
    group, artifact, version = coord.split(":")
    group_path = group.replace(".", "/")
    name = f"{artifact}-{version}"
    if classifier:
        name += f"-{classifier}"
    name += f".{ext}"
    return f"{maven_base}/{group_path}/{artifact}/{version}/{name}"


# ---------------------------------------------------------------------------
# Legacy Yarn（Legacy Fabric，1.3~1.13）：与 Yarn 同管线，端点不同
# ---------------------------------------------------------------------------
_legacy_yarn_versions_cache: Optional[set] = None
_legacy_inter_versions_cache: Optional[set] = None


def _legacy_yarn_versions() -> set:
    """一次性拉取 Legacy Fabric 全部 yarn 覆盖的游戏版本并缓存（网络失败回空集）。"""
    global _legacy_yarn_versions_cache
    if _legacy_yarn_versions_cache is None:
        try:
            data = _get_json(f"{LEGACY_FABRIC_META}/versions/yarn")
            _legacy_yarn_versions_cache = {b["gameVersion"] for b in data}
        except Exception:
            _legacy_yarn_versions_cache = set()
    return _legacy_yarn_versions_cache


def _legacy_inter_versions() -> set:
    """Legacy Fabric 有 intermediary（跨版本稳定名）的全部版本，缓存。"""
    global _legacy_inter_versions_cache
    if _legacy_inter_versions_cache is None:
        try:
            data = _get_json(f"{LEGACY_FABRIC_META}/versions/intermediary")
            _legacy_inter_versions_cache = {b["version"] for b in data}
        except Exception:
            _legacy_inter_versions_cache = set()
    return _legacy_inter_versions_cache


def _nearest_legacy_yarn(version_id: str) -> Optional[str]:
    """在同一 major.minor 内挑离 version_id 最近、且有 yarn 的版本（tie 取较高补丁）。

    intermediary 名跨版本稳定，故可借邻版 yarn 的 intermediary->named 层。
    """
    from .versions import _parse_mc_version
    tv = _parse_mc_version(version_id)
    if not tv:
        return None
    tp = tv[2] if len(tv) > 2 else 0
    cands = []
    for y in _legacy_yarn_versions():
        yv = _parse_mc_version(y)
        if yv and yv[:2] == tv[:2]:
            cands.append((y, yv[2] if len(yv) > 2 else 0))
    if not cands:
        return None
    return min(cands, key=lambda c: (abs(c[1] - tp), -c[1]))[0]


def legacy_yarn_plan(version_id: str) -> Optional[tuple[str, str, bool]]:
    """返回 (intermediary 版本, yarn 版本, 是否借用) 或 None。

    - 本版有 yarn：直接用本版（一步 official->named）。
    - 本版只有 intermediary：借同系列最近的 yarn（两步 official->intermediary->named）。
    - 两者都没有（如 1.10.1）：None，无法可读化。
    """
    if version_id in _legacy_yarn_versions():
        return (version_id, version_id, False)
    if version_id in _legacy_inter_versions():
        borrowed = _nearest_legacy_yarn(version_id)
        if borrowed:
            return (version_id, borrowed, True)
    return None


def has_legacy_yarn(version_id: str) -> bool:
    """该版本能否产出 Legacy Yarn 可读名（本版直出或借邻版 yarn）。"""
    return legacy_yarn_plan(version_id) is not None


def prepare_legacy_yarn(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    """与 prepare_yarn 读取同样的缓存文件，仅把 kind 标为 LEGACY_YARN。

    借用场景下 yarn 文件被存为 yarn-v2.jar，prepare_yarn 据此走两步重映射。
    """
    arts = prepare_yarn(entry, game_version)
    arts.kind = MappingKind.LEGACY_YARN
    return arts


def _extract_tiny_from_jar_path(jar_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar_path) as zf:
        with zf.open("mappings/mappings.tiny") as f:
            dest.write_bytes(f.read())
    return dest


def prepare_yarn(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    inter_jar = MAP_CACHE / entry.id / "intermediary-v2.jar"
    inter_tiny = MAP_CACHE / entry.id / "intermediary.tiny"
    if inter_jar.exists():
        _extract_tiny_from_jar_path(inter_jar, inter_tiny)

    merged_jar = MAP_CACHE / entry.id / "yarn-mergedv2.jar"
    v2_jar = MAP_CACHE / entry.id / "yarn-v2.jar"
    yarn_tiny = MAP_CACHE / entry.id / "yarn.tiny"

    two_step = False
    if merged_jar.exists():
        _extract_tiny_from_jar_path(merged_jar, yarn_tiny)
    elif v2_jar.exists():
        two_step = True
        _extract_tiny_from_jar_path(v2_jar, yarn_tiny)

    return MappingArtifacts(
        kind=MappingKind.YARN,
        intermediary_tiny=inter_tiny if inter_jar.exists() else None,
        yarn_tiny=yarn_tiny if yarn_tiny.exists() else None,
        yarn_two_step=two_step,
    )


# ---------------------------------------------------------------------------
# 经典 MCP（1.5.2~1.12.2）
# ---------------------------------------------------------------------------
import xml.etree.ElementTree as _ET


def _mc_base(game_version: str) -> str:
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def _list_csv_versions(channel: str) -> list[str]:
    try:
        data = _download_bytes(MCP_CSV_METADATA.format(channel=channel))
    except requests.HTTPError:
        return []
    root = _ET.fromstring(data)
    return [v.text for v in root.iter("version") if v.text]


def _best_csv(game_version: str) -> Optional[tuple[str, str]]:
    base = _mc_base(game_version)
    for channel in MCP_CSV_CHANNELS:
        versions = _list_csv_versions(channel)
        matched = [v for v in versions if _mc_base(v.split("-", 1)[-1]) == base]
        if not matched:
            continue

        def _key(v: str) -> int:
            head = v.split("-", 1)[0]
            return int(head) if head.isdigit() else 0

        return channel, max(matched, key=_key)
    return None


def prepare_mcp(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    base_dir = MAP_CACHE / entry.id
    srg_zip = base_dir / "mcp-srg.zip"
    srg_dest = base_dir / "joined.srg"

    if srg_zip.exists():
        with zipfile.ZipFile(srg_zip) as zf:
            names = zf.namelist()
            srg_name = "joined.srg" if "joined.srg" in names else next(
                (n for n in names if n.endswith(".srg")), None)
            if srg_name:
                srg_dest.write_bytes(zf.read(srg_name))

    csv_zip = base_dir / "mcp-csv.zip"
    csv_dir = base_dir / "mcp_csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    if csv_zip.exists():
        with zipfile.ZipFile(csv_zip) as zf:
            for name in ("fields.csv", "methods.csv", "params.csv"):
                if name in zf.namelist():
                    (csv_dir / name).write_bytes(zf.read(name))

    return MappingArtifacts(
        kind=MappingKind.MCP,
        mcp_srg=srg_dest,
        mcp_csv_dir=csv_dir,
    )


def has_mcp(version_id: str) -> bool:
    """该版本在 Forge maven 上是否真有现成 SRG（经典 MCP 仅对这些版本可用）。

    用精准表判断、不发网络请求，避免对一批没有 SRG 的版本逐个 404 把 Forge 打限速。
    """
    return version_id in MCP_SRG_VERSIONS


# ---------------------------------------------------------------------------
# MCP Legacy（≤1.6.4）
# ---------------------------------------------------------------------------
def has_mcp_legacy(version_id: str) -> bool:
    return version_id in MCP_LEGACY_MAPPINGS


def _pick_legacy_src_ns(tiny_path: Path) -> str:
    with open(tiny_path, "r", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
    namespaces = header[3:]
    if "official" in namespaces:
        return "official"
    if "client" in namespaces:
        return "client"
    for ns in namespaces:
        if ns != "named":
            return ns
    return namespaces[0] if namespaces else "official"


def prepare_mcp_legacy(entry: VersionEntry, version_id: str) -> MappingArtifacts:
    base_dir = MAP_CACHE / entry.id
    legacy_zip = base_dir / "legacy-mappings.zip"
    tiny_dest = base_dir / "legacy-mappings.tiny"

    if legacy_zip.exists():
        with zipfile.ZipFile(legacy_zip) as zf:
            if "mappings.tiny" in zf.namelist():
                tiny_dest.write_bytes(zf.read("mappings.tiny"))

    src_ns = _pick_legacy_src_ns(tiny_dest) if tiny_dest.exists() else "official"
    return MappingArtifacts(
        kind=MappingKind.MCP_LEGACY,
        legacy_tiny=tiny_dest,
        legacy_src_ns=src_ns,
    )