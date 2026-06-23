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
    MCP_SRG_URL, MCP_CSV_CHANNELS, MCP_CSV_METADATA, MCP_CSV_URL,
    MCP_LEGACY_MAPPINGS, SESSION,
)
from .manifest import VersionEntry


class MappingKind(enum.Enum):
    MOJANG = "mojang"
    YARN = "yarn"
    MCP = "mcp"
    MCP_LEGACY = "mcp_legacy"
    MCP_REBORN = "mcp_reborn"
    NONE = "none"

    @property
    def label(self) -> str:
        _map = {
            MappingKind.MOJANG: "Mojang official mapping",
            MappingKind.YARN: "Yarn (community, friendly names + param names)",
            MappingKind.MCP: "MCP (classic SRG + CSV names)",
            MappingKind.MCP_LEGACY: "MCP(Legacy) (RetroMCP bundled tiny mapping)",
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
    mcp_srg: Optional[Path] = None        # joined.srg (notch -> searge)
    mcp_csv_dir: Optional[Path] = None    # 含 fields.csv/methods.csv/params.csv 的目录
    legacy_tiny: Optional[Path] = None    # RetroMCP 的 mappings.tiny（混淆名 -> 可读名）
    legacy_src_ns: Optional[str] = None   # tiny 中的混淆源命名空间（official 或 client）
    yarn_two_step: bool = False           # yarn 无 mergedv2 时：official->intermediary->named 两步


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = SESSION.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


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
# Mojang official mapping
# ---------------------------------------------------------------------------
def has_mojang_mapping(vjson: dict) -> bool:
    return "client_mappings" in vjson.get("downloads", {})


def prepare_mojang(entry: VersionEntry, vjson: dict) -> MappingArtifacts:
    dl = vjson["downloads"]["client_mappings"]
    dest = MAP_CACHE / entry.id / "client_mappings.txt"
    _download(dl["url"], dest)
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


def _maven_to_url(
    coord: str, *, classifier: str | None = None, ext: str = "jar"
) -> str:
    group, artifact, version = coord.split(":")
    group_path = group.replace(".", "/")
    name = f"{artifact}-{version}"
    if classifier:
        name += f"-{classifier}"
    name += f".{ext}"
    return f"{FABRIC_MAVEN}/{group_path}/{artifact}/{version}/{name}"


def _extract_tiny_from_jar(jar_bytes: bytes, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as zf:
        with zf.open("mappings/mappings.tiny") as f:
            dest.write_bytes(f.read())
    return dest


def prepare_yarn(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    # 1) intermediary: official -> intermediary
    inter_list = _get_json(f"{FABRIC_META}/versions/intermediary/{game_version}")
    if not inter_list:
        raise ValueError(f"{game_version} has no intermediary mapping")
    inter_maven = inter_list[0]["maven"]
    inter_bytes = _download_bytes(_maven_to_url(inter_maven, classifier="v2"))
    inter_tiny = _extract_tiny_from_jar(
        inter_bytes, MAP_CACHE / entry.id / "intermediary.tiny"
    )

    # 2) yarn: 优先 mergedv2（含 official/intermediary/named 三列，可一步 official->named）；
    #    老版本（1.14.x/1.15.x）没有 mergedv2，退回 v2（仅 intermediary/named 两列），
    #    此时需要两步：先用 intermediary.tiny 做 official->intermediary，再用 yarn v2 做 intermediary->named。
    yarn_ver = best_yarn_version(game_version)
    if not yarn_ver:
        raise ValueError(f"{game_version} has no Yarn build")

    two_step = False
    try:
        yarn_bytes = _download_bytes(
            _maven_to_url(f"net.fabricmc:yarn:{yarn_ver}", classifier="mergedv2")
        )
        yarn_tiny = _extract_tiny_from_jar(
            yarn_bytes, MAP_CACHE / entry.id / "yarn-mergedv2.tiny"
        )
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        # 没有 mergedv2，退回 v2 + 两步重映射
        two_step = True
        yarn_bytes = _download_bytes(
            _maven_to_url(f"net.fabricmc:yarn:{yarn_ver}", classifier="v2")
        )
        yarn_tiny = _extract_tiny_from_jar(
            yarn_bytes, MAP_CACHE / entry.id / "yarn-v2.tiny"
        )

    return MappingArtifacts(
        kind=MappingKind.YARN,
        intermediary_tiny=inter_tiny,
        yarn_tiny=yarn_tiny,
        yarn_two_step=two_step,
    )


# ---------------------------------------------------------------------------
# 经典 MCP（1.5.2~1.12.2）：SRG（notch->searge）+ CSV（searge->可读名）
# ---------------------------------------------------------------------------
import xml.etree.ElementTree as _ET


def _mc_base(game_version: str) -> str:
    """取主版本号用于匹配 CSV 频道，如 1.12.2 -> 1.12，1.10.2 -> 1.10。"""
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def _list_csv_versions(channel: str) -> list[str]:
    """读取某 CSV 频道的 maven-metadata，返回所有版本号（如 39-1.12）。"""
    try:
        data = _download_bytes(MCP_CSV_METADATA.format(channel=channel))
    except requests.HTTPError:
        return []
    root = _ET.fromstring(data)
    return [v.text for v in root.iter("version") if v.text]


def _best_csv(game_version: str) -> Optional[tuple[str, str]]:
    """为给定 MC 版本挑选最合适的 CSV (channel, cver)。

    优先匹配精确主版本（如 1.12），stable 频道优先于 snapshot；
    同频道内取「数字前缀」最大的那个（最新映射）。
    """
    base = _mc_base(game_version)
    for channel in MCP_CSV_CHANNELS:
        versions = _list_csv_versions(channel)
        # 版本号形如 "39-1.12"/"29-1.10.2"，后缀的 major.minor 等于目标即视为匹配
        # （CSV 后缀有时 2 段 1.12、有时 3 段 1.10.2，故按 major.minor 归一）
        matched = [v for v in versions
                   if _mc_base(v.split("-", 1)[-1]) == base]
        if not matched:
            continue

        def _key(v: str) -> int:
            head = v.split("-", 1)[0]
            return int(head) if head.isdigit() else 0

        return channel, max(matched, key=_key)
    return None


def has_mcp(game_version: str) -> bool:
    try:
        r = SESSION.head(MCP_SRG_URL.format(ver=game_version),
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def prepare_mcp(entry: VersionEntry, game_version: str) -> MappingArtifacts:
    """下载并解出 MCP 的 joined.srg 与 fields/methods/params.csv。"""
    base_dir = MAP_CACHE / entry.id

    # 1) SRG：mcp-<ver>-srg.zip 内含 joined.srg（notch -> searge）
    try:
        srg_bytes = _download_bytes(MCP_SRG_URL.format(ver=game_version))
    except requests.HTTPError:
        raise ValueError(
            f"{game_version} 在 Forge maven 上没有 MCP SRG 件"
            f"（1.6.4 及更早从未发布，1.7.10~1.12.2 可用）"
        )
    srg_dest = base_dir / "joined.srg"
    srg_dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(srg_bytes)) as zf:
        names = zf.namelist()
        srg_name = "joined.srg" if "joined.srg" in names else next(
            (n for n in names if n.endswith(".srg")), None)
        if srg_name is None:
            raise ValueError(f"{game_version} 的 MCP SRG zip 内未找到 .srg 文件")
        srg_dest.write_bytes(zf.read(srg_name))

    # 2) CSV：从 stable/snapshot 频道挑最匹配的版本
    pick = _best_csv(game_version)
    if pick is None:
        raise ValueError(f"{game_version} 找不到匹配的 MCP CSV 名称映射")
    channel, cver = pick
    csv_bytes = _download_bytes(
        MCP_CSV_URL.format(channel=channel, cver=cver))
    csv_dir = base_dir / "mcp_csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(csv_bytes)) as zf:
        for name in ("fields.csv", "methods.csv", "params.csv"):
            if name in zf.namelist():
                (csv_dir / name).write_bytes(zf.read(name))

    return MappingArtifacts(
        kind=MappingKind.MCP,
        mcp_srg=srg_dest,
        mcp_csv_dir=csv_dir,
    )


# ---------------------------------------------------------------------------
# MCP(Legacy)（≤1.6.4）：RetroMCP 体系自带的 Tiny V2 映射
#   - 老版本 Forge maven 上从未发布 SRG，但 RetroMCP/RetroMCP-Legacy 直接打包了
#     混淆名 -> 可读名 的 mappings.tiny（与 Yarn 的 mergedv2 同格式），
#     因此走与 Yarn 完全相同的 tiny-remapper 管线，无需 SRG/CSV/源码替换。
#   - 仅以下版本有现成映射（见 config.MCP_LEGACY_MAPPINGS），其余 ≤1.6.4 版本无映射。
#   - tiny 头部命名空间有两种历史布局：
#       新："official named"            -> 混淆源是 official
#       旧："named client server"       -> 混淆源是 client
# ---------------------------------------------------------------------------
def has_mcp_legacy(version_id: str) -> bool:
    """该版本是否在精准映射表中（即是否有现成的 RetroMCP tiny 映射）。"""
    return version_id in MCP_LEGACY_MAPPINGS


def _pick_legacy_src_ns(tiny_path: Path) -> str:
    """读取 tiny 头部，挑出混淆名所在的源命名空间。

    Tiny V2 首行形如 `tiny\t2\t0\t<ns0>\t<ns1>...`。RetroMCP 有两种布局：
      official named            -> 用 official 作源
      named client server       -> 用 client 作源（named 在前，但它是可读名）
    """
    with open(tiny_path, "r", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
    namespaces = header[3:]
    if "official" in namespaces:
        return "official"
    if "client" in namespaces:
        return "client"
    # 兜底：取第一个非 named 的命名空间
    for ns in namespaces:
        if ns != "named":
            return ns
    return namespaces[0] if namespaces else "official"


def prepare_mcp_legacy(entry: VersionEntry, version_id: str) -> MappingArtifacts:
    """下载并解出 RetroMCP 的 mappings.tiny（混淆名 -> 可读名）。"""
    url = MCP_LEGACY_MAPPINGS.get(version_id)
    if url is None:
        raise ValueError(
            f"{version_id} 没有现成的 MCP(Legacy) 映射"
            f"（仅 {', '.join(MCP_LEGACY_MAPPINGS)} 可用）"
        )
    zip_bytes = _download_bytes(url)
    tiny_dest = MAP_CACHE / entry.id / "legacy-mappings.tiny"
    tiny_dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        if "mappings.tiny" not in zf.namelist():
            raise ValueError(f"{version_id} 的 MCP(Legacy) zip 内未找到 mappings.tiny")
        tiny_dest.write_bytes(zf.read("mappings.tiny"))
    src_ns = _pick_legacy_src_ns(tiny_dest)
    return MappingArtifacts(
        kind=MappingKind.MCP_LEGACY,
        legacy_tiny=tiny_dest,
        legacy_src_ns=src_ns,
    )