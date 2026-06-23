"""Mojang 版本清单的获取与查询。

提供：全部版本列表（含发布时间、类型）、单个版本的详细 JSON（含 jar / mapping 下载地址）。
所有结果带本地缓存，按发布时间排序的辅助函数供版本区间使用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from .config import HTTP_HEADERS, HTTP_TIMEOUT, MAP_CACHE, VERSION_MANIFEST, SESSION


@dataclass
class VersionEntry:
    """版本清单里的一条记录。"""

    id: str
    type: str           # release / snapshot / old_beta / old_alpha
    url: str            # 该版本详细 JSON 的地址
    release_time: datetime
    index: int = -1     # 在按时间正序排列后的下标，区间计算用

    @property
    def is_release(self) -> bool:
        return self.type == "release"


class Manifest:
    """封装 version_manifest_v2.json。"""

    def __init__(self, entries: list[VersionEntry], latest_release: str, latest_snapshot: str):
        self.entries = entries
        self.latest_release = latest_release
        self.latest_snapshot = latest_snapshot
        self._by_id = {e.id: e for e in entries}

    # -- 查询 ---------------------------------------------------------------
    def get(self, version_id: str) -> Optional[VersionEntry]:
        return self._by_id.get(version_id)

    def __contains__(self, version_id: str) -> bool:
        return version_id in self._by_id

    def releases(self) -> list[VersionEntry]:
        return [e for e in self.entries if e.is_release]

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def load(cls, *, force: bool = False) -> "Manifest":
        """每次都联网拉取最新版本清单（不读磁盘缓存，避免新版本/新映射看不到）。"""
        resp = SESSION.get(VERSION_MANIFEST, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        raw = data["versions"]
        # 清单本身是「最新在前」，倒过来得到时间正序，便于区间切片
        entries: list[VersionEntry] = []
        for v in raw:
            entries.append(
                VersionEntry(
                    id=v["id"],
                    type=v["type"],
                    url=v["url"],
                    release_time=datetime.fromisoformat(v["releaseTime"]),
                )
            )
        entries.sort(key=lambda e: e.release_time)
        for i, e in enumerate(entries):
            e.index = i

        return cls(
            entries=entries,
            latest_release=data["latest"]["release"],
            latest_snapshot=data["latest"]["snapshot"],
        )


def fetch_version_json(entry: VersionEntry) -> dict:
    """每次都联网获取单个版本的详细 JSON（不读磁盘缓存）。"""
    resp = SESSION.get(entry.url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
