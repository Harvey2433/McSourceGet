"""版本选择：解析单版本 / 版本区间，并按「大版本段」分组。

用户可以输入：
  - 单个版本：  1.20.4
  - 版本区间：  1.12-26.1   （含两端，按发布时间取区间内所有 release）
  - 关键字  latest / all

区间会落在很长的版本跨度上，不同大版本（1.12 / 1.16 / 1.18 / 1.20 / 1.21.11…）
适用的 mappings 不一样，因此把区间内的版本按「大版本线」分组，
让用户对每一段分别选择 mappings。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .manifest import Manifest, VersionEntry

# 用于分组的「大版本线」边界。键是该段起始的 release id，值是人类可读标签。
# 取这些有代表性的大更新作为分段锚点，落在两个锚点之间的版本归入前一个锚点段。
MAJOR_ANCHORS: list[tuple[str, str]] = [
    ("1.12", "1.12.x（多彩世界更新）"),
    ("1.13", "1.13.x（海洋更新 / 扁平化）"),
    ("1.14", "1.14.x（村庄与掠夺，首个官方 mapping）"),
    ("1.15", "1.15.x（嗡嗡蜂群）"),
    ("1.16", "1.16.x（下界更新）"),
    ("1.17", "1.17.x（洞穴与山崖 上）"),
    ("1.18", "1.18.x（洞穴与山崖 下）"),
    ("1.19", "1.19.x（荒野更新）"),
    ("1.20", "1.20.x（足迹与故事）"),
    ("1.21", "1.21.x（试炼更新）"),
    ("26.1", "26.x（取消混淆后的新版本号体系）"),
]


@dataclass
class VersionGroup:
    """一个大版本段，含落在该段内的所有版本。"""

    anchor: str          # 段锚点 id，如 "1.20"
    label: str           # 人类可读标签
    versions: list[VersionEntry] = field(default_factory=list)


def _parse_mc_version(vid: str) -> tuple[int, ...]:
    """把版本号粗解析成可比较的数字元组，无法解析的返回空元组。

    支持 1.20.4 / 1.21.11 / 26.1 这类纯数字点分版本；快照（如 24w14a）返回空。
    """
    if not re.fullmatch(r"\d+(\.\d+)*", vid):
        return ()
    return tuple(int(p) for p in vid.split("."))


def resolve_selection(manifest: Manifest, raw: str) -> list[VersionEntry]:
    """把用户输入解析为有序（按时间正序）的版本列表。"""
    raw = raw.strip()

    if raw.lower() == "latest":
        e = manifest.get(manifest.latest_release)
        return [e] if e else []

    if raw.lower() == "all":
        return manifest.releases()

    # 区间：a-b
    if "-" in raw and not raw.lower().startswith("1.0-"):
        lo, _, hi = raw.partition("-")
        lo, hi = lo.strip(), hi.strip()
        lo_e, hi_e = manifest.get(lo), manifest.get(hi)
        if lo_e is None:
            raise ValueError(f"未找到起始版本：{lo}")
        if hi_e is None:
            raise ValueError(f"未找到结束版本：{hi}")
        i, j = sorted((lo_e.index, hi_e.index))
        # 区间内只取正式版（release），避免一次性拉进上千个快照
        return [e for e in manifest.entries[i : j + 1] if e.is_release]

    # 单个版本（可以是快照）
    e = manifest.get(raw)
    if e is None:
        raise ValueError(f"未找到版本：{raw}")
    return [e]


def group_by_major(versions: list[VersionEntry]) -> list[VersionGroup]:
    """把版本列表按大版本段分组，仅返回非空的段，保持时间顺序。"""
    groups: dict[str, VersionGroup] = {}
    order: list[str] = []

    # 预解析锚点的数字形式用于比较
    anchors: list[tuple[tuple[int, ...], str, str]] = []
    for aid, label in MAJOR_ANCHORS:
        anchors.append((_parse_mc_version(aid), aid, label))

    for v in versions:
        nums = _parse_mc_version(v.id)
        chosen = anchors[0]
        if nums:
            # 找到 <= 当前版本号的最大锚点（anchors 已按版本升序）
            for a in anchors:
                if a[0] and nums >= a[0]:
                    chosen = a
        else:
            # 快照无法数字解析，归到最后一个锚点段
            chosen = anchors[-1]

        _, aid, label = chosen
        if aid not in groups:
            groups[aid] = VersionGroup(anchor=aid, label=label)
            order.append(aid)
        groups[aid].versions.append(v)

    return [groups[a] for a in order]
