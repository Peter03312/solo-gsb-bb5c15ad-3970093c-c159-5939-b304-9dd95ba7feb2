"""基于 SQLAlchemy 的目录修订持久化。

修订可见性
----------
每个版本记录 ``published_revision`` 与 ``revoked_revision``：
在修订 R 可见 ⇔ published_revision <= R 且
（未撤销 或 R < revoked_revision）。

ORM 相关导入延迟到函数内，使本模块可在没有 sqlalchemy 的环境下被加载
（测试可用内存实现替换这些函数，见 tests/test_catalog_flow.py）。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Iterable

from . import catalog
from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import SpecifierSet, Version


def _orm():
    """延迟加载 ORM 符号。"""
    from sqlalchemy import func, select
    from .models import (
        CatalogRevision, Conflict, Dependency, Plugin, PluginVersion, Solution,
        VersionRuntime,
    )
    return SimpleNamespace(
        func=func, select=select, CatalogRevision=CatalogRevision,
        Conflict=Conflict, Dependency=Dependency, Plugin=Plugin,
        PluginVersion=PluginVersion, Solution=Solution,
        VersionRuntime=VersionRuntime,
    )


# ---------------------------------------------------------------- 修订

def latest_revision(db: Any) -> int:
    o = _orm()
    return db.execute(
        o.select(o.func.coalesce(o.func.max(o.CatalogRevision.revision), 0))
    ).scalar_one()


def bump_revision(db: Any, *, kind: str, plugin: str | None,
                  version: str | None, note: str | None = None):
    o = _orm()
    row = o.CatalogRevision(kind=kind, plugin=plugin, version=version, note=note)
    db.add(row)
    db.flush()  # 取自增修订号；同事务内串行，保证单调
    return row


# ---------------------------------------------------------------- 发布 / 撤销

def publish_version(db: Any, *, metadata, src_path: str,
                    original_filename: str, package_dir: str):
    """同事务：校验 -> 查重 -> 摘要校验 -> 落盘 -> 写行 -> 修订 +1。"""
    o = _orm()
    catalog.validate_metadata_specs(metadata)

    plugin = db.execute(
        o.select(o.Plugin).where(o.Plugin.name == metadata.plugin)
    ).scalar_one_or_none()
    if plugin is None:
        plugin = o.Plugin(name=metadata.plugin)
        db.add(plugin)
        db.flush()

    existing = db.execute(
        o.select(o.PluginVersion).where(
            o.PluginVersion.plugin_id == plugin.id,
            o.PluginVersion.version == metadata.version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise catalog.PublishError(
            f"{metadata.plugin}=={metadata.version} 已发布（修订 "
            f"{existing.published_revision}），同名同版本不可改写；"
            "如已撤销请发布新版本号")

    catalog.verify_sha256(src_path, metadata.sha256)

    safe_name = os.path.basename(original_filename) or "package.bin"
    storage_path, size = catalog.save_package_atomic(
        src_path, metadata.sha256, package_dir)

    revision = bump_revision(
        db, kind="publish", plugin=metadata.plugin, version=metadata.version)

    pv = o.PluginVersion(
        plugin_id=plugin.id,
        version=metadata.version,
        sha256=metadata.sha256,
        filename=safe_name,
        size_bytes=size,
        storage_path=storage_path,
        published_revision=revision.revision,
    )
    db.add(pv)
    db.flush()

    for dep in metadata.dependencies:
        db.add(o.Dependency(source_id=pv.id, dep_plugin=dep.plugin,
                            spec=dep.spec or ""))
    for rt in metadata.runtimes:
        db.add(o.VersionRuntime(version_id=pv.id, name=rt.name,
                                spec=rt.spec or ""))
    for name in metadata.conflicts:
        db.add(o.Conflict(source_id=pv.id, conflict_plugin=name))

    db.flush()
    return pv, revision.revision


def revoke_version(db: Any, *, plugin_name: str, version: str,
                   reason: str | None) -> int:
    o = _orm()
    pv = db.execute(
        o.select(o.PluginVersion)
        .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
        .where(o.Plugin.name == plugin_name, o.PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None:
        raise catalog.PublishError(f"未找到 {plugin_name}=={version}")
    if pv.revoked_revision is not None:
        raise catalog.PublishError(
            f"{plugin_name}=={version} 已在修订 {pv.revoked_revision} 撤销")

    revision = bump_revision(
        db, kind="revoke", plugin=plugin_name, version=version, note=reason)
    pv.revoked_revision = revision.revision
    pv.revoke_reason = reason
    db.flush()
    return revision.revision


# ---------------------------------------------------------------- 快照

def load_snapshot(db: Any, revision: int) -> Snapshot:
    o = _orm()
    versions = list(db.execute(
        o.select(o.PluginVersion, o.Plugin.name)
        .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
        .where(
            o.PluginVersion.published_revision <= revision,
            (o.PluginVersion.revoked_revision.is_(None))
            | (o.PluginVersion.revoked_revision > revision),
        )
    ).all())

    ids = [pv.id for pv, _ in versions]
    rel = _load_relations(db, ids)

    grouped: dict[str, list[Candidate]] = {}
    for pv, plugin_name in versions:
        runtimes = tuple(
            RuntimeRequirement(name=r.name, spec=SpecifierSet.parse(r.spec))
            for r in rel["runtimes"].get(pv.id, [])
        )
        deps = tuple(
            (d.dep_plugin, SpecifierSet.parse(d.spec))
            for d in sorted(rel["deps"].get(pv.id, []),
                            key=lambda d: d.dep_plugin)
        )
        conflicts = tuple(
            sorted(c.conflict_plugin for c in rel["conflicts"].get(pv.id, [])))
        grouped.setdefault(plugin_name, []).append(Candidate(
            plugin=plugin_name,
            version=Version.parse(pv.version),
            runtimes=runtimes,
            dependencies=deps,
            conflicts=conflicts,
        ))

    return Snapshot(
        revision=revision,
        candidates_by_plugin={
            name: tuple(sorted(cs, key=lambda c: c.version, reverse=True))
            for name, cs in grouped.items()
        },
    )


def _load_relations(db: Any, ids: list[int]) -> dict:
    o = _orm()
    result = {"deps": {}, "runtimes": {}, "conflicts": {}}
    if not ids:
        return result
    for d in db.execute(
            o.select(o.Dependency).where(o.Dependency.source_id.in_(ids))).scalars():
        result["deps"].setdefault(d.source_id, []).append(d)
    for r in db.execute(
            o.select(o.VersionRuntime).where(
                o.VersionRuntime.version_id.in_(ids))).scalars():
        result["runtimes"].setdefault(r.version_id, []).append(r)
    for c in db.execute(
            o.select(o.Conflict).where(o.Conflict.source_id.in_(ids))).scalars():
        result["conflicts"].setdefault(c.source_id, []).append(c)
    return result


# ---------------------------------------------------------------- 方案

def get_solution(db: Any, revision: int, request_hash: str):
    o = _orm()
    return db.execute(
        o.select(o.Solution).where(
            o.Solution.revision_id == revision,
            o.Solution.request_hash == request_hash,
        )
    ).scalar_one_or_none()


def save_solution(db: Any, *, revision: int, request_hash: str,
                  request: dict, selection: dict, install_order: list[str]):
    o = _orm()
    sol = o.Solution(
        revision_id=revision,
        request_hash=request_hash,
        request=request,
        selection=selection,
        install_order=install_order,
    )
    db.add(sol)
    db.flush()
    return sol


def revoked_flags(db: Any, version_ids: Iterable[int],
                  as_of_revision: int | None = None) -> dict[int, bool]:
    """版本撤销标记。

    ``as_of_revision`` 为空：返回**当前**是否已撤销（历史方案查询用，
    已撤销版本会带 revoked=true）。给定修订号 R：返回该版本在修订 R
    时点是否已撤销（求解/预演选到的版本在 R 必可见，恒为 false）。
    """
    o = _orm()
    ids = list(set(version_ids))
    if not ids:
        return {}
    rows = db.execute(
        o.select(o.PluginVersion.id, o.PluginVersion.revoked_revision)
        .where(o.PluginVersion.id.in_(ids))
    ).all()
    if as_of_revision is None:
        return {row[0]: row[1] is not None for row in rows}
    return {row[0]: row[1] is not None and row[1] <= as_of_revision
            for row in rows}
