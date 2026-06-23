"""自动下载并管理反编译/反混淆工具。

管理三个外部 Java 工具：
  - Vineflower（反编译器）
  - tiny-remapper（Yarn 路线的字节码重映射）
  - SpecialSource（Mojang mapping 路线的字节码重映射）

所有工具缓存在 ~/.mcsourceget/tools/ 下，只下载一次。
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from .config import (
    HTTP_HEADERS,
    HTTP_TIMEOUT,
    SPECIALSOURCE_JAR,
    SPECIALSOURCE_URL,
    TINY_REMAPPER_JAR,
    TINY_REMAPPER_URL,
    VINEFLOWER_JAR,
    VINEFLOWER_URL,
)


def _find_java() -> str:
    java = shutil.which("java")
    if java is None:
        raise RuntimeError(
            "未找到 java，请安装 JDK 21+ 并确保 java 在 PATH 中"
        )
    return java


JAVA = _find_java()


def _download_tool(url: str, dest: Path, label: str) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        if zipfile.is_zipfile(dest):
            return dest
        else:
            dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(".tmp")

    resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))

    # 配合全局改为 Rust 风格
    with Progress(
        TextColumn("[bold green]{task.fields[action]:>12}[/]"),
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
    ) as progress:
        task = progress.add_task(label, total=total, action="Downloading")
        with open(tmp_dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                f.write(chunk)
                progress.update(task, advance=len(chunk))

    shutil.move(str(tmp_dest), str(dest))
    return dest


def ensure_vineflower() -> Path:
    return _download_tool(VINEFLOWER_URL, VINEFLOWER_JAR, "Vineflower")


def ensure_tiny_remapper() -> Path:
    return _download_tool(TINY_REMAPPER_URL, TINY_REMAPPER_JAR, "tiny-remapper")


def ensure_specialsource() -> Path:
    return _download_tool(SPECIALSOURCE_URL, SPECIALSOURCE_JAR, "SpecialSource")


def run_java(
    args: list[str],
    *,
    cwd: Path | None = None,
    jvm_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """执行 java 命令。jvm_args 在 -jar 之前注入（如 -Xmx 限堆）。"""
    cmd = [JAVA] + (jvm_args or []) + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Java 命令执行失败 (exit {result.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {result.stderr[:2000]}"
        )
    return result