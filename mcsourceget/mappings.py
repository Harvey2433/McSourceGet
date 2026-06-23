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
    MCP_LEGACY_MAPPINGS, LEGACY_FABRIC_META, LEGACY_FABRIC_MAVEN, SESSION,
)
from .manifest import VersionEntry


class MappingKind(enum.Enum):
    MOJANG = "mojang"
    YARN = "yarn"
    LEGACY_YARN = "legacy_yarn"
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
        downloads += _resolve_yarn_like(
            entry, LEGACY_FABRIC_META, LEGACY_FABRIC_MAVEN, "net.legacyfabric")
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


def has_legacy_yarn(version_id: str) -> bool:
    """该版本是否有 Legacy Fabric 的 yarn（可读名）映射。"""
    return version_id in _legacy_yarn_versions()


def prepare_legacy_yarn(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    """与 prepare_yarn 读取同样的缓存文件，仅把 kind 标为 LEGACY_YARN。"""
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