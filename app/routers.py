"""HTTP 路由。

事务隔离
--------
发布/撤销使用默认 READ COMMITTED 事务并立即提交，提交后产生新目录修订。
求解使用 REPEATABLE READ 快照：一次方案从头到尾只读同一目录修订，
并发发布/撤销不会让方案混用不同修订；并发事务插入冲突方案时由
(uq_solution_request) 去重并重读。
"""
from __future__ import annotations

import json
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import catalog, repository, service
from .config import settings
from .database import get_db
from .models import CatalogRevision, Plugin, PluginVersion, Solution
from .schemas import (
    PreviewOut, PreviewRequest, PublishMetadata, RevisionOut, RevokeIn,
    SolutionOut, SolveRequest, VersionOut,
)
from .service import RequestError, SolutionNotFound, annotate_revoked
from .solver import CycleError, UnsatisfiableError
from .version import InvalidSpecifier

router = APIRouter()


def _safe_request_hash(body: SolveRequest) -> str:
    """对原始（已通过 pydantic 校验）的请求计算规范化哈希，供错误回显。"""
    targets = (body.targets if isinstance(body.targets, dict)
               else {d.plugin: d.spec for d in body.targets})
    try:
        runtime_reqs = service._parse_runtimes(body.runtimes)
        h, _ = catalog.canonical_request(runtime_reqs, targets)
        return h
    except RequestError:
        return ""


# ------------------------------------------------------------ 鉴权（可选）

def _check_token(authorization: str | None) -> None:
    token = settings.api_token
    if not token:
        return
    got = ""
    if authorization and authorization.startswith("Bearer "):
        got = authorization[len("Bearer "):].strip()
    if got != token:
        raise HTTPException(status_code=401, detail="缺少或无效的 API Token")


# ------------------------------------------------------------ 发布

@router.post("/plugins/{plugin_name}/versions", response_model=VersionOut,
             status_code=201, tags=["publish"])
async def publish(
    plugin_name: str,
    metadata: str = Form(..., description="发布元数据 JSON 字符串"),
    package: UploadFile = File(..., description="插件安装包"),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    _check_token(authorization)
    try:
        data = json.loads(metadata)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="metadata 不是合法 JSON")
    try:
        meta = PublishMetadata(**data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=_pydantic_errors(e))
    if meta.plugin != plugin_name:
        raise HTTPException(
            status_code=400,
            detail=f"路径插件名 {plugin_name} 与元数据 {meta.plugin} 不一致")

    # 流式落到临时文件，限制大小
    fd, tmp_path = tempfile.mkstemp(prefix="upload-", suffix=".pkg")
    too_large = False
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await package.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    too_large = True
                    break
                out.write(chunk)
        if too_large:
            raise HTTPException(
                status_code=413,
                detail=f"安装包超过上限 {settings.max_upload_bytes} 字节")

        try:
            pv, _revision_no = repository.publish_version(
                db, metadata=meta, src_path=tmp_path,
                original_filename=package.filename or "package.bin",
                package_dir=settings.package_dir,
            )
            db.commit()
        except catalog.PublishError as e:
            db.rollback()
            # 重复版本 409；摘要/范围/互斥自身等 422
            status = 409 if ("不可改写" in str(e) or "已发布" in str(e)) else 422
            raise HTTPException(status_code=status, detail=str(e))
        except IntegrityError:
            db.rollback()
            # 极端并发下的唯一约束竞争
            raise HTTPException(status_code=409, detail="同名同版本已存在（并发冲突）")
    finally:
        await package.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    db.refresh(pv)
    return _version_out(db, pv)


# ------------------------------------------------------------ 撤销

@router.post("/plugins/{plugin_name}/versions/{version}/revoke",
             response_model=VersionOut, tags=["publish"])
def revoke(plugin_name: str, version: str, body: RevokeIn | None = None,
           db: Session = Depends(get_db),
           authorization: str | None = Header(default=None)):
    _check_token(authorization)
    try:
        repository.revoke_version(
            db, plugin_name=plugin_name, version=version,
            reason=(body.reason if body else None))
        db.commit()
    except catalog.PublishError as e:
        db.rollback()
        status = 409 if ("已" in str(e)) else 404
        raise HTTPException(status_code=status, detail=str(e))

    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one()
    return _version_out(db, pv)


# ------------------------------------------------------------ 目录浏览

@router.get("/revisions", response_model=list[RevisionOut], tags=["catalog"])
def list_revisions(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    rows = db.execute(
        select(CatalogRevision).order_by(CatalogRevision.revision.desc()).limit(limit)
    ).scalars()
    return [RevisionOut(
        revision=r.revision, created_at=r.created_at, kind=r.kind,
        plugin=r.plugin, version=r.version, note=r.note) for r in rows]


@router.get("/revisions/{revision}", response_model=RevisionOut, tags=["catalog"])
def get_revision(revision: int, db: Session = Depends(get_db)):
    r = db.get(CatalogRevision, revision)
    if r is None:
        raise HTTPException(404, f"修订 {revision} 不存在")
    return RevisionOut(
        revision=r.revision, created_at=r.created_at, kind=r.kind,
        plugin=r.plugin, version=r.version, note=r.note)


@router.get("/plugins", tags=["catalog"])
def list_plugins(revision: int | None = None, db: Session = Depends(get_db)):
    """列出插件及其版本；给定 revision 时只显示该修订下未撤销版本。"""
    if revision is None:
        base_rev = repository.latest_revision(db)
    else:
        base_rev = revision
        if db.get(CatalogRevision, revision) is None:
            raise HTTPException(404, f"修订 {revision} 不存在")
    pvs = db.execute(
        select(PluginVersion, Plugin.name)
        .join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(
            PluginVersion.published_revision <= base_rev,
            (PluginVersion.revoked_revision.is_(None))
            | (PluginVersion.revoked_revision > base_rev),
        )
        .order_by(Plugin.name, PluginVersion.published_revision)
    ).all()
    out: dict[str, list[str]] = {}
    for pv, name in pvs:
        out.setdefault(name, []).append(pv.version)
    return {"revision": base_rev, "plugins": out}


@router.get("/plugins/{plugin_name}/versions/{version}",
            response_model=VersionOut, tags=["catalog"])
def get_version(plugin_name: str, version: str, db: Session = Depends(get_db)):
    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None:
        raise HTTPException(404, f"未找到 {plugin_name}=={version}")
    return _version_out(db, pv)


@router.get("/plugins/{plugin_name}/versions/{version}/download",
            tags=["catalog"], include_in_schema=True)
def download(plugin_name: str, version: str, db: Session = Depends(get_db)):
    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None or not os.path.exists(pv.storage_path):
        raise HTTPException(404, "安装包不存在")
    return FileResponse(
        pv.storage_path, filename=pv.filename,
        headers={
            "X-SHA256": pv.sha256,
            "X-Revoked": "true" if pv.revoked_revision else "false",
        },
    )


# ------------------------------------------------------------ 求解

@router.post("/solutions", response_model=SolutionOut, tags=["solve"])
def solve(body: SolveRequest, db: Session = Depends(get_db),
          authorization: str | None = Header(default=None)):
    _check_token(authorization)

    # 关键：求解必须固定在同一目录修订上。
    # PostgreSQL 用 REPEATABLE READ 事务快照；其它方言（测试用 SQLite）
    # 的单写连接天然等价，跳过该选项以免方言报错。
    _set_snapshot_isolation(db)

    try:
        revision = service.resolve_revision(db, body.revision)
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    request_hash = _safe_request_hash(body)

    try:
        outcome = service.execute(
            db,
            revision=revision,
            runtimes_in=body.runtimes,
            targets_in=body.targets,
        )
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except (UnsatisfiableError, CycleError) as e:
        db.rollback()
        code = "dependency_cycle" if isinstance(e, CycleError) else "unsatisfiable"
        status = 422 if isinstance(e, CycleError) else 409
        raise HTTPException(status_code=status, detail={
            "detail": str(e),
            "code": code,
            "revision": revision,
            "request_hash": request_hash,
            "conflicts": e.reasons,
        })
    except InvalidSpecifier as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    try:
        db.commit()
    except Exception as exc:
        # 并发下唯一约束冲突（两个事务同时算出同一方案）：重读既有方案
        db.rollback()
        existing = repository.get_solution(db, outcome.revision, request_hash)
        if existing is None:
            raise HTTPException(status_code=500, detail=f"方案提交失败: {exc}")
        outcome = type(outcome)(
            solution_row=existing, cached=True,
            selection=existing.selection, install_order=existing.install_order,
            revision=outcome.revision)

    row = outcome.solution_row
    selection = annotate_revoked(db, outcome.selection, outcome.revision)
    return SolutionOut(
        solution_id=row.id,
        revision=outcome.revision,
        cached=outcome.cached,
        request_hash=row.request_hash,
        selection=_ordered_selection(selection),
        install_order=outcome.install_order,
        created_at=row.created_at,
    )


@router.get("/solutions/{solution_id}", response_model=SolutionOut, tags=["solve"])
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    row = db.get(Solution, solution_id)
    if row is None:
        raise HTTPException(404, f"方案 {solution_id} 不存在")
    selection = annotate_revoked(db, row.selection)
    return SolutionOut(
        solution_id=row.id,
        revision=row.revision_id,
        cached=True,
        request_hash=row.request_hash,
        selection=_ordered_selection(selection),
        install_order=row.install_order,
        created_at=row.created_at,
    )


# ------------------------------------------------------------ 方案预演（不保存）

def _set_snapshot_isolation(db: Session) -> None:
    """预演/求解固定在同一目录修订快照上（PostgreSQL REPEATABLE READ）。"""
    if db.bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _safe_preview_request_hash(row: Solution | None, body: PreviewRequest) -> str:
    """对预演的合并后有效请求计算规范化哈希，供失败回显；解析失败返回空串。"""
    if row is None:
        return ""
    try:
        (_, targets_plain, runtime_reqs, _, _) = service.effective_request(
            row, runtimes_in=body.runtimes, targets_in=body.targets)
        h, _ = catalog.canonical_request(runtime_reqs, targets_plain)
        return h
    except RequestError:
        return ""


@router.post("/solutions/{solution_id}/preview", response_model=PreviewOut,
             tags=["solve"])
def preview_solution(solution_id: int, body: PreviewRequest,
                     db: Session = Depends(get_db)):
    """对已保存方案在目标修订上做只读预演：最小变动优先，不保存任何结果。"""
    _set_snapshot_isolation(db)

    try:
        revision = service.resolve_revision(db, body.revision)
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    row = db.get(Solution, solution_id)
    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail=f"方案 {solution_id} 不存在")

    request_hash = _safe_preview_request_hash(row, body)

    try:
        outcome = service.preview(
            db,
            solution_id=solution_id,
            revision=revision,
            runtimes_in=body.runtimes,
            targets_in=body.targets,
        )
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except SolutionNotFound as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except (UnsatisfiableError, CycleError) as e:
        db.rollback()
        code = "dependency_cycle" if isinstance(e, CycleError) else "unsatisfiable"
        status = 422 if isinstance(e, CycleError) else 409
        raise HTTPException(status_code=status, detail={
            "detail": str(e),
            "code": code,
            "revision": revision,
            "source_solution_id": solution_id,
            "request_hash": request_hash,
            "conflicts": e.reasons,
        })
    except InvalidSpecifier as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    # 预演不写任何东西：显式回滚结束只读事务
    db.rollback()

    selection = annotate_revoked(db, outcome.selection, outcome.revision)
    return PreviewOut(
        source_solution_id=outcome.source_solution_id,
        revision=outcome.revision,
        saved=False,
        request_hash=outcome.request_hash,
        runtimes=outcome.effective_runtimes,
        targets=outcome.effective_targets,
        selection=_ordered_selection(selection),
        install_order=outcome.install_order,
        change_counts=outcome.change_counts,
        changes={k: outcome.changes[k] for k in sorted(outcome.changes)},
    )


# ------------------------------------------------------------ 辅助

def _ordered_selection(selection: dict) -> dict:
    """pydantic 模型按插件名有序输出，保证响应确定性。"""
    return {k: selection[k] for k in sorted(selection)}


def _pydantic_errors(exc: ValueError):
    # PublishMetadata(**data) 抛出的校验错误可能是 ValidationError
    errs = getattr(exc, "errors", None)
    if callable(errs):
        try:
            return errs()
        except Exception:
            pass
    return str(exc)


def _version_out(db: Session, pv: PluginVersion) -> VersionOut:
    plugin = db.get(Plugin, pv.plugin_id)
    return VersionOut(
        plugin=plugin.name,
        version=pv.version,
        sha256=pv.sha256,
        filename=pv.filename,
        size_bytes=pv.size_bytes,
        runtimes=[{"name": r.name, "spec": r.spec} for r in
                  sorted(pv.runtimes, key=lambda x: x.name)],
        dependencies=[{"plugin": d.dep_plugin, "spec": d.spec} for d in
                      sorted(pv.dependencies, key=lambda x: x.dep_plugin)],
        conflicts=sorted(c.conflict_plugin for c in pv.conflicts),
        published_revision=pv.published_revision,
        revoked_revision=pv.revoked_revision,
        revoke_reason=pv.revoke_reason,
        revoked=pv.revoked_revision is not None,
        created_at=pv.created_at,
    )
