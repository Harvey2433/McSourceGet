"""自动下载并管理反编译/反混淆工具。

管理三个外部 Java 工具：
  - Vineflower（反编译器）
  - tiny-remapper（Yarn 路线的字节码重映射）
  - SpecialSource（Mojang mapping 路线的字节码重映射）

由 CLI 主流线统一进行并行网络拉取，此处仅提供断言与调用封装。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import (
    SPECIALSOURCE_JAR,
    TINY_REMAPPER_JAR,
    VINEFLOWER_JAR,
)


def _find_java() -> str:
    java = shutil.which("java")
    if java is None:
        raise RuntimeError(
            "未找到 java，请安装 JDK 21+ 并确保 java 在 PATH 中"
        )
    return java


JAVA = _find_java()


def ensure_vineflower() -> Path:
    if not VINEFLOWER_JAR.exists():
        raise FileNotFoundError(f"工具缺失: Vineflower 未被统一拉取成功 ({VINEFLOWER_JAR})")
    return VINEFLOWER_JAR


def ensure_tiny_remapper() -> Path:
    if not TINY_REMAPPER_JAR.exists():
        raise FileNotFoundError(f"工具缺失: tiny-remapper 未被统一拉取成功 ({TINY_REMAPPER_JAR})")
    return TINY_REMAPPER_JAR


def ensure_specialsource() -> Path:
    if not SPECIALSOURCE_JAR.exists():
        raise FileNotFoundError(f"工具缺失: SpecialSource 未被统一拉取成功 ({SPECIALSOURCE_JAR})")
    return SPECIALSOURCE_JAR


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