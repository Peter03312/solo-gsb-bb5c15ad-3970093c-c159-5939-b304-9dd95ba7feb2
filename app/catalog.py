"""目录修订领域逻辑（不依赖任何 Web/ORM 框架，便于离线测试）。

包含：
* 发布元数据校验（版本号、范围、互斥、重复依赖）
* 安装包 SHA-256 校验与内容寻址原子落盘
* 请求规范化哈希（同请求同哈希）

ORM 持久化操作见 :mod:`app.repository`。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

__all__ = [
    "PublishError", "validate_metadata_specs", "verify_sha256",
    "save_package_atomic", "canonical_request",
]


class PublishError(ValueError):
    """发布被拒绝（摘要不符、范围非法、重复版本等）。"""


# ---------------------------------------------------------------- 校验

def validate_metadata_specs(metadata) -> None:
    """发布前校验：版本号合法、所有范围可解析、互斥项不含自身、依赖不重复。"""
    try:
        Version.parse(metadata.version)
    except InvalidVersion as e:
        raise PublishError(str(e)) from e

    for dep in metadata.dependencies:
        try:
            SpecifierSet.parse(dep.spec or "")
        except InvalidSpecifier as e:
            raise PublishError(f"依赖 {dep.plugin} 的版本范围无效: {e}") from e

    for rt in metadata.runtimes:
        try:
            SpecifierSet.parse(rt.spec or "")
        except InvalidSpecifier as e:
            raise PublishError(f"运行环境 {rt.name} 的版本范围无效: {e}") from e

    dep_names = [d.plugin for d in metadata.dependencies]
    if len(set(dep_names)) != len(dep_names):
        raise PublishError("同一依赖插件只能声明一条版本范围")

    if metadata.plugin in metadata.conflicts:
        raise PublishError("插件不能与自身互斥")


def verify_sha256(path: str, expected: str) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected.lower():
        raise PublishError(
            f"安装包摘要不符：声明 {expected}，实际 {actual}，已拒绝写入")


def save_package_atomic(src_path: str, sha256: str, dest_dir: str) -> tuple[str, int]:
    """以 sha256 前两字符分桶做内容寻址，先写临时文件再原子重命名。"""
    os.makedirs(dest_dir, exist_ok=True)
    bucket = os.path.join(dest_dir, sha256[:2])
    os.makedirs(bucket, exist_ok=True)
    final_path = os.path.join(bucket, sha256)
    size = os.path.getsize(src_path)
    if not os.path.exists(final_path):
        tmp_path = os.path.join(bucket, f".tmp-{os.getpid()}-{sha256}")
        try:
            shutil.move(src_path, tmp_path)
            os.replace(tmp_path, final_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return final_path, size


# ---------------------------------------------------------------- 请求规范化

def canonical_request(runtimes, targets: dict[str, str]) -> tuple[str, dict]:
    """规范化请求并计算 SHA-256：同请求（忽略键顺序）得到相同哈希。"""
    payload = {
        "runtimes": sorted(
            ({"name": r.name, "spec": str(r.spec)} for r in runtimes),
            key=lambda x: x["name"],
        ),
        "targets": {k: (v or "") for k, v in sorted(targets.items())},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), payload
