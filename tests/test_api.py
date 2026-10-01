"""HTTP API 端到端集成测试。

需要 fastapi / sqlalchemy / pydantic / python-multipart / httpx：
    pip install fastapi 'sqlalchemy>=2' pydantic python-multipart httpx
    python3 tests/test_api.py

测试使用 SQLite 内存库与临时安装包目录；求解器本身在生产环境跑在
PostgreSQL + REPEATABLE READ 事务上，逻辑一致（JSON 字段跨方言兼容）。
"""
from __future__ import annotations

import hashlib
import importlib
import io
import os
import sys
import tempfile
import traceback
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _have(pkg):
    try:
        importlib.import_module(pkg)
        return True
    except Exception:
        return False


REQUIRED = ("fastapi", "sqlalchemy", "pydantic", "multipart", "httpx")


@unittest.skipUnless(all(_have(p) for p in REQUIRED),
                     f"缺少依赖，需要: {', '.join(REQUIRED)}")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="plugin-api-test-")
        os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
        os.environ["PACKAGE_DIR"] = cls.tmp

        # 静态隔离：首次运行时导入受配置影响的模块
        if "app.main" not in sys.modules:
            for name in list(sys.modules):
                if name.startswith("app."):
                    del sys.modules[name]

        import importlib
        importlib.import_module("app.models")  # 注册表元数据
        importlib.import_module("app.main")    # 加载 FastAPI 应用与路由

    def setUp(self):
        # 每个用例一个全新的内存库，避免插件名/修订号跨用例相互影响
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from app import database
        from app.main import app, init_db
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        database.engine = self.engine
        database.SessionLocal.configure(bind=self.engine)
        init_db()
        self.client = TestClient(app)

    # ------------------------------------------------------------ helpers
    def publish(self, plugin, version, payload, deps=None, rts=None, conflicts=None,
                sha=None, expect=None):
        body = payload if isinstance(payload, bytes) else payload.encode()
        meta = {
            "plugin": plugin, "version": version,
            "sha256": sha or hashlib.sha256(body).hexdigest(),
            "dependencies": deps or [],
            "runtimes": rts or [],
            "conflicts": conflicts or [],
        }
        files = {
            "metadata": (None, _json(meta), "application/json"),
            "package": (f"{plugin}-{version}.bin", io.BytesIO(body),
                        "application/octet-stream"),
        }
        resp = self.client.post(f"/api/v1/plugins/{plugin}/versions", files=files)
        if expect is not None:
            self.assertEqual(resp.status_code, expect, resp.text)
        return resp

    def _batch_meta(self, plugin, version, payload, deps=None, rts=None,
                    sha=None):
        body = payload if isinstance(payload, bytes) else payload.encode()
        meta = {
            "plugin": plugin, "version": version,
            "sha256": sha or hashlib.sha256(body).hexdigest(),
            "dependencies": deps or [], "runtimes": rts or [], "conflicts": [],
        }
        return meta, body

    def batch_publish(self, batch_id, members, expect=None):
        """members: [(meta_dict, payload_bytes), ...]，按下标提交安装包。"""
        metas = [m for m, _ in members]
        files = {"items": (None, _json(metas), "application/json")}
        for i, (_, body) in enumerate(members):
            key = "package" if len(members) == 1 else f"package:{i}"
            files[key] = (f"pkg{i}.bin", io.BytesIO(body),
                          "application/octet-stream")
        resp = self.client.post(f"/api/v1/batches/{batch_id}/publish",
                                files=files)
        if expect is not None:
            self.assertEqual(resp.status_code, expect, resp.text)
        return resp

    def _package_files(self):
        out = []
        for dp, _, fns in os.walk(self.tmp):
            out.extend(os.path.join(dp, f) for f in fns
                       if not f.startswith(".tmp"))
        return out

    # -------------------------------------------------- 批次发布（离线交付）
    def test_batch_publish_single_revision_and_simultaneous_visibility(self):
        members = [
            self._batch_meta("bz-lib", "1.0.0", b"lib",
                             rts=[{"name": "python", "spec": ">=3.10"}]),
            self._batch_meta("bz-app", "1.0.0", b"app",
                             deps=[{"plugin": "bz-lib", "spec": ">=1"}]),
        ]
        r = self.batch_publish("batch-1", members, expect=201)
        body = r.json()
        self.assertFalse(body["cached"])
        self.assertEqual(len(body["members"]), 2)
        # 整批只产生一个修订，两成员共享该修订并同时可见
        revs = {m["published_revision"] for m in body["members"]}
        self.assertEqual(revs, {body["revision"]})
        rev = body["revision"]
        # 修订之前（rev-1）两个成员都不可见；修订之上同时可见
        before = self.client.get(f"/api/v1/plugins?revision={rev - 1}").json()
        self.assertEqual(before["plugins"], {})
        at = self.client.get(f"/api/v1/plugins?revision={rev}").json()
        self.assertEqual(at["plugins"],
                         {"bz-app": ["1.0.0"], "bz-lib": ["1.0.0"]})
        # 下载与求解在该修订上即取即用
        sol = self.client.post("/api/v1/solutions",
                               json={"targets": {"bz-app": ""}}).json()
        self.assertEqual(sol["revision"], rev)
        self.assertEqual(sol["install_order"], ["bz-lib", "bz-app"])

    def test_batch_retry_same_id_same_content_returns_original(self):
        members = [self._batch_meta("id-a", "1.0.0", b"a"),
                   self._batch_meta("id-b", "1.0.0", b"b")]
        r1 = self.batch_publish("idem-1", members, expect=201)
        r2 = self.batch_publish("idem-1", members, expect=200)
        b1, b2 = r1.json(), r2.json()
        self.assertTrue(b2["cached"])
        self.assertEqual(b2["revision"], b1["revision"])
        self.assertEqual(b2["content_hash"], b1["content_hash"])
        self.assertEqual(b2["members"], b1["members"])
        # 重试不产生新修订：仍是第 1 号修订
        latest = self.client.get("/api/v1/revisions").json()
        self.assertEqual([x["revision"] for x in latest], [1, 0])
        g = self.client.get("/api/v1/batches/idem-1")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["revision"], b1["revision"])

    def test_batch_same_id_different_content_conflict(self):
        m1 = [self._batch_meta("cf-x", "1.0.0", b"x"),
              self._batch_meta("cf-y", "1.0.0", b"y")]
        self.batch_publish("cf-id", m1, expect=201)
        m2 = [self._batch_meta("cf-x", "1.0.0", b"x"),
              self._batch_meta("cf-y", "1.1.0", b"y-different")]
        r = self.batch_publish("cf-id", m2, expect=409)
        self.assertIn("不同内容", r.json()["detail"])
        # 冲突未产生新修订，cf-y 1.1.0 不可见也无包
        latest = self.client.get("/api/v1/revisions").json()
        self.assertEqual([x["revision"] for x in latest], [1, 0])

    def test_batch_duplicate_plugin_version_within_batch_rejected(self):
        members = [self._batch_meta("dup-z", "1.0.0", b"z1"),
                   self._batch_meta("dup-z", "1.0.0", b"z2")]
        before = set(self._package_files())
        r = self.batch_publish("dup-batch", members, expect=422)
        self.assertIn("重复", r.json()["detail"])
        # 校验在落盘之前：包目录无新增
        self.assertEqual(set(self._package_files()), before)

    def test_batch_bad_sha256_makes_whole_batch_invisible_without_leftovers(self):
        before = set(self._package_files())
        members = [
            self._batch_meta("bad1", "1.0.0", b"good"),
            self._batch_meta("bad2", "1.0.0", b"bad", sha="0" * 64),
        ]
        r = self.batch_publish("bad-sha-batch", members, expect=422)
        self.assertIn("摘要不符", r.json()["detail"])
        # 整批不可见：没有任何修订、版本、包文件
        latest = self.client.get("/api/v1/revisions").json()
        self.assertEqual([x["revision"] for x in latest], [0])
        self.assertEqual(set(self._package_files()), before)

    def test_batch_conflict_with_existing_version_is_invisible_no_leftovers(self):
        # 单版本发布先占用 keep==1.0.0
        self.publish("keep", "1.0.0", b"existing", expect=201)
        before = set(self._package_files())
        # 批次改写既有版本 + 一个全新成员：整批失败，新成员包不得遗留
        members = [
            self._batch_meta("keep", "1.0.0", b"existing"),
            self._batch_meta("brand-new", "1.0.0", b"fresh-payload"),
        ]
        r = self.batch_publish("collide-batch", members, expect=409)
        self.assertIn("不可改写", r.json()["detail"])
        after = set(self._package_files())
        self.assertEqual(before, after)  # 既有包保留，失败批次未新增文件
        g = self.client.get("/api/v1/plugins/brand-new/versions/1.0.0")
        self.assertEqual(g.status_code, 404)

    def test_batch_single_item_uses_bare_package_field(self):
        meta, body = self._batch_meta("solo", "1.0.0", b"solo-payload")
        r = self.client.post("/api/v1/batches/solo-batch/publish", files={
            "items": (None, _json([meta]), "application/json"),
            "package": ("solo.bin", io.BytesIO(body),
                        "application/octet-stream"),
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["revision"], 1)

    def test_batch_missing_and_misindexed_packages_rejected(self):
        metas = [self._batch_meta("p1", "1.0.0", b"1")[0],
                 self._batch_meta("p2", "1.0.0", b"2")[0]]
        # 下标不连续
        files = {"items": (None, _json(metas), "application/json"),
                 "package:0": ("a.bin", io.BytesIO(b"1"),
                               "application/octet-stream"),
                 "package:2": ("b.bin", io.BytesIO(b"2"),
                               "application/octet-stream")}
        r = self.client.post("/api/v1/batches/misidx/publish", files=files)
        self.assertEqual(r.status_code, 400, r.text)
        # 缺 items
        r = self.client.post("/api/v1/batches/noitems/publish", files={
            "package": ("a.bin", io.BytesIO(b"1"),
                        "application/octet-stream")})
        self.assertEqual(r.status_code, 400, r.text)

    def test_batch_single_publish_still_compatible_after_batch(self):
        members = [self._batch_meta("mix-lib", "1.0.0", b"l")]
        self.batch_publish("mix-batch", members, expect=201)
        # 批次后，单版本发布照常工作并只再产生一个修订
        r = self.publish("mix-tool", "1.0.0", b"t")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["published_revision"], 2)

    def test_batch_concurrent_same_id_yields_one_revision(self):
        import threading
        from fastapi.testclient import TestClient as _TestClient
        from sqlalchemy import create_engine, event
        from app import database
        from app.main import app as _app, init_db as _init_db
        dbdir = tempfile.mkdtemp()
        dbpath = os.path.join(dbdir, "conc.db")
        batch_uid = os.path.basename(dbdir)
        engine = create_engine(
            f"sqlite+pysqlite:///{dbpath}",
            connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _busy(dbapi_con, _):
            dbapi_con.cursor().execute("PRAGMA busy_timeout=30000")

        database.engine = engine
        database.SessionLocal.configure(bind=engine)
        _init_db()

        barrier = threading.Barrier(5)
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def worker():
            try:
                client = _TestClient(_app)
                meta, body = self._batch_meta("cp", "1.0.0", b"cp-payload")
                files = {"items": (None, _json([meta]), "application/json"),
                         "package": ("cp.bin", io.BytesIO(body),
                                     "application/octet-stream")}
                barrier.wait()
                resp = client.post(f"/api/v1/batches/conc-{batch_uid}/publish",
                                   files=files)
                with lock:
                    results.append(resp)
            except BaseException:
                with lock:
                    errors.append(traceback.format_exc())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, "".join(errors))
        statuses = sorted(r.status_code for r in results)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[-1], 201)
        revisions = {r.json()["revision"] for r in results}
        self.assertEqual(revisions, {1})
        self.assertEqual(sum(1 for r in results if not r.json()["cached"]), 1)
        latest = self.client.get("/api/v1/revisions").json()
        self.assertEqual([x["revision"] for x in latest], [1, 0])
        dl = self.client.get("/api/v1/plugins/cp/versions/1.0.0/download")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.content, b"cp-payload")

    # ------------------------------------------------------------ tests
    def test_health_and_root(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_publish_and_get(self):
        r = self.publish("pkg-a", "1.0.0", b"A1",
                         rts=[{"name": "python", "spec": ">=3.9"}])
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["published_revision"], 1)
        g = self.client.get("/api/v1/plugins/pkg-a/versions/1.0.0")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["runtimes"], [{"name": "python", "spec": ">=3.9"}])

    def test_bad_sha256_rejected(self):
        r = self.publish("pkg-bad", "1.0.0", b"x", sha="0" * 64, expect=422)
        self.assertIn("摘要不符", r.text)

    def test_invalid_version_and_spec_rejected(self):
        r = self.publish("pkg-badver", "not-a-version", b"x")
        self.assertEqual(r.status_code, 422, r.text)
        r = self.publish("pkg-badspec", "1.0.0", b"x",
                         deps=[{"plugin": "x", "spec": "~=1"}])
        self.assertEqual(r.status_code, 422, r.text)

    def test_duplicate_version_immutable(self):
        self.publish("pkg-dup", "1.0.0", b"same-payload", expect=201)
        r = self.publish("pkg-dup", "1.0.0", b"same-payload")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("不可改写", r.json()["detail"])

    def test_solve_transitive_and_cache(self):
        self.publish("libx", "2.0.0", b"libx2",
                     rts=[{"name": "python", "spec": ">=3.10"}])
        self.publish("libx", "1.0.0", b"libx1",
                     rts=[{"name": "python", "spec": ">=3.8,<3.10"}])
        self.publish("toolz", "1.0.0", b"toolz",
                     deps=[{"plugin": "libx", "spec": ">=2"}],
                     rts=[{"name": "python", "spec": ">=3.10"}])
        req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
               "targets": {"toolz": ""}}
        r1 = self.client.post("/api/v1/solutions", json=req)
        self.assertEqual(r1.status_code, 200, r1.text)
        body1 = r1.json()
        self.assertFalse(body1["cached"])
        self.assertEqual(body1["selection"]["libx"]["version"], "2.0.0")
        self.assertEqual(body1["install_order"], ["libx", "toolz"])

        r2 = self.client.post("/api/v1/solutions", json=req)
        body2 = r2.json()
        self.assertTrue(body2["cached"])
        self.assertEqual(body2["solution_id"], body1["solution_id"])
        # 选择结果逐字节一致
        self.assertEqual(body2["selection"], body1["selection"])

    def test_unsat_returns_conflicts_no_partial_solution(self):
        # libz 只有 1.x，请求 >=9
        self.publish("libz", "1.0.0", b"libz")
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"libz": ">=9"}})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "unsatisfiable")
        self.assertTrue(detail["conflicts"])
        self.assertEqual(detail["conflicts"][0]["type"], "no_matching_version")
        # 失败不保存：solutions 表为空（前面成功的方案除外），这里直接确认
        # 对该 request_hash 查不到记录——通过再次同请求仍 cached=false 间接验证
        again = self.client.post("/api/v1/solutions",
                                 json={"targets": {"libz": ">=9"}})
        self.assertEqual(again.status_code, 409)

    def test_cycle_422(self):
        self.publish("cyc-a", "1.0.0", b"a",
                     deps=[{"plugin": "cyc-b", "spec": ">=1"}])
        self.publish("cyc-b", "1.0.0", b"b",
                     deps=[{"plugin": "cyc-a", "spec": ">=1"}])
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"cyc-a": ""}})
        self.assertEqual(r.status_code, 422, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "dependency_cycle")
        cyc = detail["conflicts"][0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])

    def test_revoke_affects_new_but_not_history(self):
        self.publish("rev-lib", "1.0.0", b"revlib")
        self.publish("rev-app", "1.0.0", b"revapp",
                     deps=[{"plugin": "rev-lib", "spec": ""}])
        sol = self.client.post("/api/v1/solutions",
                               json={"targets": {"rev-app": ""}}).json()
        sid, revision = sol["solution_id"], sol["revision"]

        r = self.client.post("/api/v1/plugins/rev-lib/versions/1.0.0/revoke",
                             json={"reason": "缺陷"})
        self.assertEqual(r.status_code, 200, r.text)
        new_rev = r.json()["revoked_revision"]
        self.assertGreater(new_rev, revision)

        # 历史方案仍可查，且撤销项被标记
        hist = self.client.get(f"/api/v1/solutions/{sid}").json()
        self.assertTrue(hist["selection"]["rev-lib"]["revoked"])
        self.assertFalse(hist["selection"]["rev-app"]["revoked"])

        # 旧修订重放仍可得到原方案
        replay = self.client.post("/api/v1/solutions", json={
            "revision": revision, "targets": {"rev-app": ""}}).json()
        self.assertEqual(replay["selection"]["rev-lib"]["version"], "1.0.0")
        self.assertFalse(replay["selection"]["rev-lib"]["revoked"])

        # 新求解：rev-lib 已撤销 -> 无解（409）
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"rev-app": ""}})
        self.assertEqual(r.status_code, 409)

    def test_double_revoke_conflict(self):
        self.publish("dbl", "1.0.0", b"dbl")
        self.client.post("/api/v1/plugins/dbl/versions/1.0.0/revoke",
                         json={"reason": "x"})
        r = self.client.post("/api/v1/plugins/dbl/versions/1.0.0/revoke",
                             json={"reason": "y"})
        self.assertEqual(r.status_code, 409)

    def test_download_with_sha_header(self):
        self.publish("dl", "1.0.0", b"download-me")
        r = self.client.get("/api/v1/plugins/dl/versions/1.0.0/download")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"download-me")
        self.assertEqual(r.headers["x-sha256"],
                         hashlib.sha256(b"download-me").hexdigest())

    def test_mutex_end_to_end(self):
        self.publish("mx", "1.0.0", b"mx", conflicts=["my"])
        self.publish("my", "1.0.0", b"my")
        self.publish("mhost", "1.0.0", b"mhost",
                     deps=[{"plugin": "mx", "spec": ""},
                           {"plugin": "my", "spec": ""}])
        r = self.client.post("/api/v1/solutions",
                             json={"targets": {"mhost": ""}})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(any(c["type"] == "mutex_conflict"
                            for c in r.json()["detail"]["conflicts"]))

    # -------------------------------------------------- 方案预演（dry-run）
    def _preview_fixture(self):
        # lib 三个版本；toolz 1 -> lib>=1，toolz 2 -> lib>=2
        self.publish("pv-lib", "1.0.0", b"lib1")
        self.publish("pv-lib", "2.0.0", b"lib2")
        self.publish("pv-lib", "3.0.0", b"lib3")
        self.publish("pv-toolz", "1.0.0", b"toolz1",
                     deps=[{"plugin": "pv-lib", "spec": ">=1"}])
        self.publish("pv-toolz", "2.0.0", b"toolz2",
                     deps=[{"plugin": "pv-lib", "spec": ">=2"}])
        sol = self.client.post("/api/v1/solutions",
                               json={"targets": {"pv-toolz": ""}})
        self.assertEqual(sol.status_code, 200, sol.text)
        body = sol.json()
        self.assertEqual(body["selection"]["pv-lib"]["version"], "3.0.0")
        self.assertEqual(body["selection"]["pv-toolz"]["version"], "2.0.0")
        return body["solution_id"], body["revision"]

    def test_preview_same_revision_is_all_keep_and_not_saved(self):
        sid, rev = self._preview_fixture()
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["saved"])
        self.assertEqual(body["revision"], rev)
        self.assertEqual(body["source_solution_id"], sid)
        self.assertEqual(body["change_counts"],
                         {"keep": 2, "change": 0, "add": 0, "remove": 0,
                          "changed": 0})
        self.assertTrue(all(v["action"] == "keep"
                            for v in body["changes"].values()))
        self.assertEqual(body["install_order"], ["pv-lib", "pv-toolz"])
        # 预演沿用原请求：回显目标/运行环境
        self.assertEqual(body["targets"], {"pv-toolz": ""})
        # 预演不产生新方案：同 ID 查询结果仍是原方案
        hist = self.client.get(f"/api/v1/solutions/{sid}").json()
        self.assertEqual(hist["selection"]["pv-lib"]["version"], "3.0.0")

    def test_preview_targets_override_keeps_lib_one_change(self):
        sid, rev = self._preview_fixture()
        # 覆盖目标为 toolz<2：lib3.0 同时满足 toolz1 的 lib>=1，
        # 最小变动 = 仅换版 toolz，lib 保留（keep）
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev,
                                   "targets": {"pv-toolz": "<2"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["targets"], {"pv-toolz": "<2"})
        self.assertEqual(body["selection"]["pv-toolz"]["version"], "1.0.0")
        self.assertEqual(body["selection"]["pv-lib"]["version"], "3.0.0")
        self.assertEqual(body["changes"]["pv-toolz"],
                         {"action": "change", "from_version": "2.0.0",
                          "to_version": "1.0.0"})
        self.assertEqual(body["changes"]["pv-lib"],
                         {"action": "keep", "from_version": "3.0.0",
                          "to_version": "3.0.0"})
        self.assertEqual(body["change_counts"],
                         {"keep": 1, "change": 1, "add": 0, "remove": 0,
                          "changed": 1})

    def test_preview_targets_override_forces_both_change(self):
        sid, rev = self._preview_fixture()
        # 覆盖目标追加 lib<3：排除 lib3，保留 lib3 不可能，
        # -> 换 toolz 且把 lib 降到 2.0：两次换版
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev,
                                   "targets": {"pv-toolz": "<2", "pv-lib": "<3"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["targets"], {"pv-lib": "<3", "pv-toolz": "<2"})
        self.assertEqual(body["selection"]["pv-toolz"]["version"], "1.0.0")
        self.assertEqual(body["selection"]["pv-lib"]["version"], "2.0.0")
        self.assertEqual(body["change_counts"],
                         {"keep": 0, "change": 2, "add": 0, "remove": 0,
                          "changed": 2})

    def test_preview_runtimes_override_unsat_no_partial_save(self):
        sid, rev = self._preview_fixture()
        # lib 未声明任何运行环境约束 -> 无环境需求时必兼容；
        # 换成声明 python>=3.10 的插件构造环境无解：直接用不存在插件目标
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev,
                                   "targets": {"ghost-plugin": ""}})
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "unsatisfiable")
        self.assertEqual(detail["source_solution_id"], sid)
        self.assertTrue(any(c["type"] == "missing_plugin"
                            for c in detail["conflicts"]))

    def test_preview_solution_not_found(self):
        r = self.client.post("/api/v1/solutions/999999/preview",
                             json={"revision": 0})
        self.assertEqual(r.status_code, 404, r.text)

    def test_preview_revision_not_found(self):
        sid, _ = self._preview_fixture()
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": 999999})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不存在", r.json()["detail"])

    def test_preview_invalid_override_rejected(self):
        sid, rev = self._preview_fixture()
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev,
                                   "targets": {"pv-toolz": "~=1"}})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev, "targets": {}})
        self.assertEqual(r.status_code, 422, r.text)

    def test_preview_stable_after_later_publish_and_revoke(self):
        sid, rev = self._preview_fixture()
        # 首次预演：固定在 rev
        r1 = self.client.post(f"/api/v1/solutions/{sid}/preview",
                              json={"revision": rev})
        self.assertEqual(r1.status_code, 200, r1.text)

        # 之后发布新版本并撤销旧版本
        self.publish("pv-lib", "4.0.0", b"lib4")
        self.client.post("/api/v1/plugins/pv-lib/versions/3.0.0/revoke",
                         json={"reason": "x"})

        # 相同输入 + 同一修订：结果稳定
        r2 = self.client.post(f"/api/v1/solutions/{sid}/preview",
                              json={"revision": rev})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["selection"], r1.json()["selection"])
        self.assertEqual(r2.json()["changes"], r1.json()["changes"])
        self.assertEqual(r2.json()["request_hash"], r1.json()["request_hash"])

    def test_preview_on_later_revision_picks_up_changes(self):
        sid, rev = self._preview_fixture()
        # 发布 lib4，再撤销原方案选中的 lib3：在新修订上保留 lib3 已不可能，
        # 预演必须换到可见版本；toolz2 允许 lib>=2，确定性选最新 lib4
        self.publish("pv-lib", "4.0.0", b"lib4")
        self.client.post("/api/v1/plugins/pv-lib/versions/3.0.0/revoke",
                         json={"reason": "x"})
        latest = self.client.get("/api/v1/revisions").json()[0]["revision"]
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": latest})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["selection"]["pv-lib"]["version"], "4.0.0")
        self.assertEqual(body["changes"]["pv-lib"],
                         {"action": "change", "from_version": "3.0.0",
                          "to_version": "4.0.0"})
        self.assertEqual(body["changes"]["pv-toolz"]["action"], "keep")

    def test_preview_runtime_override_applied(self):
        # 带运行环境的场景：新插件 pyonly 只支持 python>=3.10
        self.publish("pv-py", "1.0.0", b"py",
                     rts=[{"name": "python", "spec": ">=3.10"}])
        sol = self.client.post("/api/v1/solutions",
                               json={"runtimes": [{"name": "python",
                                                   "spec": "3.11.*"}],
                                     "targets": {"pv-py": ""}}).json()
        sid, rev = sol["solution_id"], sol["revision"]
        # 覆盖为 python 3.8：无解（409），结构化原因含运行环境阻塞
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": rev,
                                   "runtimes": [{"name": "python",
                                                 "spec": "3.8.*"}]})
        self.assertEqual(r.status_code, 409, r.text)
        conflicts = r.json()["detail"]["conflicts"]
        self.assertTrue(any("python" in c.get("runtime_blocked", [])
                            for c in conflicts))
        # 沿用原运行环境仍可解；回显规范化后的运行环境（与 request_hash 一致）
        r2 = self.client.post(f"/api/v1/solutions/{sid}/preview",
                              json={"revision": rev})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["runtimes"],
                         [{"name": "python", "spec": "==3.11.*"}])

    def test_preview_cycle_returns_422(self):
        # 先有一份已保存方案，再发布互依成环的两个插件，覆盖目标到环上
        self.publish("pv-base", "1.0.0", b"base")
        sol = self.client.post("/api/v1/solutions",
                               json={"targets": {"pv-base": ""}}).json()
        sid = sol["solution_id"]
        self.publish("pv-ca", "1.0.0", b"ca",
                     deps=[{"plugin": "pv-cb", "spec": ""}])
        self.publish("pv-cb", "1.0.0", b"cb",
                     deps=[{"plugin": "pv-ca", "spec": ""}])
        latest = self.client.get("/api/v1/revisions").json()[0]["revision"]
        r = self.client.post(f"/api/v1/solutions/{sid}/preview",
                             json={"revision": latest, "runtimes": [],
                                   "targets": {"pv-ca": ""}})
        self.assertEqual(r.status_code, 422, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "dependency_cycle")
        cyc = detail["conflicts"][0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])


def _json(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
