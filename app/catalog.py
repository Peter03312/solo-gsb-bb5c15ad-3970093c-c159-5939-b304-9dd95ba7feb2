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
import re
import shutil

from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

__all__ = [
    "PublishError", "BatchConflictError", "validate_metadata_specs",
    "validate_batch_id", "validate_batch_items", "canonical_batch_payload",
    "batch_content_hash", "verify_sha256", "save_package_atomic",
    "stage_package_atomic", "canonical_request",
]


class PublishError(ValueError):
    """发布被拒绝（摘要不符、范围非法、重复版本等）。"""


class BatchConflictError(PublishError):
    """相同批次标识但提交内容不同（HTTP 409，区别于单项校验失败的 422）。"""


BATCH_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def validate_batch_id(batch_id: str) -> str:
    batch_id = (batch_id or "").strip()
    if not BATCH_ID_RE.match(batch_id):
        raise PublishError(
            "批次标识只允许字母、数字、. _ -（最长 128），且不能为空")
    return batch_id


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
    """以 sha256 前两字符分桶做内容寻址，先写临时文件再原子重命名。

    返回 ``(最终路径, 字节数)``；目标已存在（相同内容的包此前已落盘）时
    直接复用，调用方无法据此区分新旧——批次回滚请改用
    :func:`stage_package_atomic`。
    """
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


def stage_package_atomic(src_path: str, sha256: str,
                         dest_dir: str) -> tuple[str, int, bool]:
    """批次专用：落盘并明确告知文件是否由本次调用新建。

    内容寻址命中既有文件（相同 sha256 的包此前已成功发布）时不移动源文件、
    返回 ``created=False``；新建返回 ``created=True``，供整批失败时只清理
    “由失败批次引入”的包文件，绝不删除此前已存在的同内容包。
    """
    os.makedirs(dest_dir, exist_ok=True)
    bucket = os.path.join(dest_dir, sha256[:2])
    os.makedirs(bucket, exist_ok=True)
    final_path = os.path.join(bucket, sha256)
    if os.path.exists(final_path):
        return final_path, os.path.getsize(final_path), False
    size = os.path.getsize(src_path)
    tmp_path = os.path.join(bucket, f".tmp-{os.getpid()}-{sha256}")
    try:
        shutil.move(src_path, tmp_path)
        os.replace(tmp_path, final_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return final_path, size, True


# ---------------------------------------------------------------- 批次

def validate_batch_items(items) -> None:
    """批次提交前校验：非空、(插件, 版本) 在批内不重复，且逐项沿用单版本发布
    的全部校验（版本号、版本范围、SHA-256 形态、互斥、重复依赖）。"""
    if not items:
        raise PublishError("批次至少要包含一个发布项")
    keys: set[tuple[str, str]] = set()
    for i, meta in enumerate(items):
        validate_metadata_specs(meta)
        key = (meta.plugin, meta.version)
        if key in keys:
            raise PublishError(
                f"批次内插件名与版本组合重复：{key[0]}=={key[1]}")
        keys.add(key)


def canonical_batch_payload(items) -> list[dict]:
    """批次内容的规范化形态：按 (插件, 版本) 排序，逐项与单版本发布同构。

    只取决于各成员的元数据内容，与提交顺序无关，使同标识重试可判等。
    """
    payload = []
    for meta in items:
        payload.append({
            "plugin": meta.plugin,
            "version": meta.version,
            "sha256": meta.sha256,
            "runtimes": sorted(
                ({"name": r.name, "spec": str(SpecifierSet.parse(r.spec or ""))}
                 for r in meta.runtimes),
                key=lambda x: x["name"]),
            "dependencies": sorted(
                ({"plugin": d.plugin, "spec": str(SpecifierSet.parse(d.spec or ""))}
                 for d in meta.dependencies),
                key=lambda x: x["plugin"]),
            "conflicts": list(meta.conflicts),
        })
    payload.sort(key=lambda m: (m["plugin"], m["version"]))
    return payload


def batch_content_hash(items) -> str:
    """批次内容哈希：相同成员内容（忽略顺序）哈希一致，任一成员不同即冲突。"""
    raw = json.dumps(canonical_batch_payload(items), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
