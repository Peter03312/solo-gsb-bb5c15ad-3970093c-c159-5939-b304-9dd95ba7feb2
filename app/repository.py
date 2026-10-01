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
        CatalogRevision, Conflict, Dependency, Plugin, PluginVersion,
        PublishBatch, Solution, VersionRuntime,
    )
    return SimpleNamespace(
        func=func, select=select, CatalogRevision=CatalogRevision,
        Conflict=Conflict, Dependency=Dependency, Plugin=Plugin,
        PluginVersion=PluginVersion, PublishBatch=PublishBatch,
        Solution=Solution, VersionRuntime=VersionRuntime,
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


# ---------------------------------------------------------------- 批次发布

def get_batch(db: Any, batch_id: str):
    o = _orm()
    return db.execute(
        o.select(o.PublishBatch).where(o.PublishBatch.batch_id == batch_id)
    ).scalar_one_or_none()


def sha256_referenced(db: Any, sha_values: Iterable[str]) -> set[str]:
    """返回已提交版本行仍在引用（按内容 sha256）的子集。

    批次失败清理包文件时必须保留这些内容：并发同内容重试的赢家可能已经
    提交并指向同一个内容寻址文件。
    """
    values = list(sha_values)
    if not values:
        return set()
    o = _orm()
    rows = db.execute(
        o.select(o.PluginVersion.sha256)
        .where(o.PluginVersion.sha256.in_(values)).distinct()
    ).all()
    return {r[0] for r in rows}


def publish_batch(db: Any, *, batch_id: str, items: list,
                  src_paths: list[str], original_filenames: list[str],
                  package_dir: str):
    """整批一个事务：全部校验通过才落盘/写库并只产生**一个**目录修订。

    成功返回 ``(revision_no, members, cached, staged_new)``；``staged_new``
    为本次调用新建的包最终路径（缓存命中为空），供提交（commit）失败回滚后
    精确删除。相同 ``batch_id`` + 相同内容重试返回首次结果（``cached=True``，
    不产生新修订）；标识相同内容不同抛 :class:`catalog.BatchConflictError`。

    任何校验失败都在落盘/写修订之前抛出；落盘后到提交前失败时，本函数负责
    删除仅由本次批次新建的包文件（内容寻址命中的既有文件不动），保证不留
    下仅由失败批次引入的包。
    """
    o = _orm()
    catalog.validate_batch_id(batch_id)
    catalog.validate_batch_items(items)
    if len(items) != len(src_paths):
        raise catalog.PublishError(
            f"批次元数据 {len(items)} 项与安装包 {len(src_paths)} 个不一致")

    existing_batch = get_batch(db, batch_id)
    content_hash = catalog.batch_content_hash(items)
    if existing_batch is not None:
        if existing_batch.content_hash != content_hash:
            raise catalog.BatchConflictError(
                f"批次标识 {batch_id} 已用于不同内容的提交（修订 "
                f"{existing_batch.revision_id}）；相同标识不得提交不同内容")
        return existing_batch.revision_id, existing_batch.members, True, []

    # 先复核全部 SHA-256（源文件尚在临时目录），任一不符即整体拒绝，
    # 此时尚未落任何包、未写修订。并发重试时上一轮可能已把源文件移入内容
    # 寻址存储（随后整事务回滚）：源不在但同 sha 的最终文件已在，则复核最终
    # 文件即可；两者都不在才视为安装包缺失。
    for meta, src in zip(items, src_paths):
        final = os.path.join(package_dir, meta.sha256[:2], meta.sha256)
        if not os.path.exists(src):
            if os.path.exists(final):
                catalog.verify_sha256(final, meta.sha256)
                continue
            raise catalog.PublishError(
                f"{meta.plugin}=={meta.version} 的安装包在提交过程中丢失，请重新提交")
        catalog.verify_sha256(src, meta.sha256)

    # 关键：先占修订并**尽早插入批次幂等行**，再做与既有版本的冲突检查与落盘。
    # 相同标识的并发重试在此由唯一约束立刻分胜负：输家整事务回滚，绝不会走到
    # 下面的版本冲突检查，因此“走到版本检查仍命中已存在版本”只可能是与不相干
    # 的更早发布（或另一个不同标识批次）撞名，属确定的不可改写冲突。
    # 输掉时的修订号随回滚消失（PG 序列只留空洞，修订号仍单调）。
    revision = bump_revision(db, kind="batch_publish", plugin=None,
                             version=None, note=f"批次 {batch_id}")
    rev_no = revision.revision
    members = [{
        "plugin": meta.plugin,
        "version": meta.version,
        "sha256": meta.sha256,
        "package_url": (f"/api/v1/plugins/{meta.plugin}/versions/"
                        f"{meta.version}/download"),
        "published_revision": rev_no,
    } for meta in items]
    batch_row = o.PublishBatch(
        batch_id=batch_id, content_hash=content_hash,
        revision_id=rev_no, members=members)
    db.add(batch_row)
    db.flush()  # 同标识并发竞争在这里被唯一约束序列化

    # 批内 (插件, 版本) 不重复（validate_batch_items 已保证）；
    # 与已发布版本冲突则逐项给出同名同版本不可改写错误。走到这里时同标识竞争
    # 已被上面的唯一约束排除，命中只可能是不相干的既有版本。
    names = {m.plugin for m in items}
    plugins = {p.name: p for p in db.execute(
        o.select(o.Plugin).where(o.Plugin.name.in_(sorted(names)))).scalars()}
    existing_versions: dict[str, set[str]] = {n: set() for n in names}
    for pv, pname in db.execute(
            o.select(o.PluginVersion, o.Plugin.name)
            .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
            .where(o.Plugin.name.in_(sorted(names)))).all():
        existing_versions.setdefault(pname, set()).add(pv.version)
    for meta in items:
        if meta.version in existing_versions.get(meta.plugin, set()):
            raise catalog.PublishError(
                f"{meta.plugin}=={meta.version} 已发布，同名同版本不可改写；"
                "批次内不允许改写既有版本")

    # 内容寻址落盘；记录仅由本次新建的最终路径，失败时交给路由层回滚后清理。
    staged_new: list[str] = []
    try:
        new_plugin_rows: dict[str, object] = dict(plugins)
        for meta, src, filename in zip(items, src_paths, original_filenames):
            final_path, size, created = catalog.stage_package_atomic(
                src, meta.sha256, package_dir)
            if created:
                staged_new.append(final_path)

            plugin = new_plugin_rows.get(meta.plugin)
            if plugin is None:
                plugin = o.Plugin(name=meta.plugin)
                db.add(plugin)
                db.flush()
                new_plugin_rows[meta.plugin] = plugin
            pv = o.PluginVersion(
                plugin_id=plugin.id,
                version=meta.version,
                sha256=meta.sha256,
                filename=os.path.basename(filename) or "package.bin",
                size_bytes=size,
                storage_path=final_path,
                published_revision=rev_no,
            )
            db.add(pv)
            db.flush()
            for dep in meta.dependencies:
                db.add(o.Dependency(source_id=pv.id, dep_plugin=dep.plugin,
                                    spec=dep.spec or ""))
            for rt in meta.runtimes:
                db.add(o.VersionRuntime(version_id=pv.id, name=rt.name,
                                        spec=rt.spec or ""))
            for cname in meta.conflicts:
                db.add(o.Conflict(source_id=pv.id, conflict_plugin=cname))
    except BaseException as exc:
        # 落盘后、提交前失败：不在事务内删除文件（此刻无法判断并发赢家是否
        # 已引用同一内容）。把本次新建路径挂在异常上，由路由层回滚后用
        # sha256_referenced 复核再安全清理，避免误删并发赢家的内容寻址文件。
        exc.staged_new = staged_new  # type: ignore[attr-defined]
        raise

    return rev_no, members, False, staged_new


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
