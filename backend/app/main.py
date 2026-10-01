"""FastAPI 入口：多标注语料冲突裁决台。"""
from __future__ import annotations

import json

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from . import service
from .db import get_conn, init_db
from .schemas import (
    AnnotationSubmission,
    AnnotatorIn,
    LabelIn,
    OverrideIn,
    ProjectIn,
    RelationIn,
    parse_tokens,
)

app = FastAPI(title="多标注语料冲突裁决台", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ── 通用辅助 ──────────────────────────────────────────────────────────
def _row(conn, sql: str, args=()):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise HTTPException(404, "资源不存在")
    return row


def _state_for_doc(conn, doc_id: int):
    doc = _row(conn, "SELECT * FROM documents WHERE id=?", (doc_id,))
    project = _row(conn, "SELECT * FROM projects WHERE id=?", (doc["project_id"],))
    state = service.load_document_state(conn, dict(doc))
    return dict(doc), dict(project), state


def _refresh_adj(conn, adj_id: int) -> dict:
    adj = _row(conn, "SELECT * FROM adjudications WHERE id=?", (adj_id,))
    if adj["frozen"]:
        raise HTTPException(409, "该裁决已生成报告并冻结，不能修改（旧报告不可变）。")
    doc, _project, state = _state_for_doc(conn, adj["document_id"])
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM overrides WHERE adjudication_id=? ORDER BY id", (adj_id,)
        )
    ]
    result = service.recompute(conn, doc, state, rows)
    conn.execute(
        "UPDATE adjudications SET result_json=?, updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), service.now_iso(), adj_id),
    )
    conn.commit()
    return result


# ── 项目 ──────────────────────────────────────────────────────────────
@app.post("/api/projects")
def create_project(body: ProjectIn):
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO projects(name, created_at) VALUES(?,?)",
            (body.name, service.now_iso()),
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": body.name}
    finally:
        conn.close()


@app.get("/api/projects")
def list_projects():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]
    finally:
        conn.close()


@app.get("/api/projects/{pid}")
def project_overview(pid: int):
    conn = get_conn()
    try:
        project = _row(conn, "SELECT * FROM projects WHERE id=?", (pid,))
        docs = [
            dict(r)
            for r in conn.execute(
                "SELECT id,name,is_active,imported_at,length(content) AS content_length "
                "FROM documents WHERE project_id=? ORDER BY id",
                (pid,),
            )
        ]
        labels = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM labels WHERE project_id=? ORDER BY id", (pid,)
            )
        ]
        rels = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM label_relations WHERE project_id=? ORDER BY id", (pid,)
            )
        ]
        ann = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM annotators WHERE project_id=? ORDER BY id", (pid,)
            )
        ]
        return {
            "project": dict(project),
            "documents": docs,
            "labels": labels,
            "relations": rels,
            "annotators": ann,
        }
    finally:
        conn.close()


# ── 文档导入 ───────────────────────────────────────────────────────────
@app.post("/api/projects/{pid}/documents")
async def import_document(
    pid: int,
    name: str | None = Form(None),
    content: str | None = Form(None),
    tokens_text: str | None = Form(None),
    doc_file: UploadFile | None = File(None),
    tokens_file: UploadFile | None = File(None),
):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM projects WHERE id=?", (pid,))

        # 浏览器对空 <input type=file> 也会发送空部件，需按文件名/内容判空。
        doc_raw = b""
        if doc_file is not None and doc_file.filename:
            doc_raw = await doc_file.read()

        if doc_raw:
            try:
                file_content = doc_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(422, f"文档不是合法 UTF-8: {exc}") from exc
            doc_name = name or doc_file.filename or "导入文档"
        elif content is not None and content:
            file_content = content
            doc_name = name or "粘贴文档"
        else:
            raise HTTPException(422, "请提供 UTF-8 文档文件，或填写 content 字段。")

        if tokens_file is not None and tokens_file.filename:
            tokens_raw = (await tokens_file.read()).decode("utf-8")
        elif tokens_text is not None and tokens_text.strip():
            tokens_raw = tokens_text
        else:
            raise HTTPException(422, "请提供词元边界 JSON 文件或 tokens_text 字段。")

        try:
            tokens = parse_tokens(tokens_raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        if not file_content:
            raise HTTPException(422, "文档内容为空。")
        clen = len(file_content)
        if tokens[-1][1] > clen:
            raise HTTPException(422, f"词元终点 {tokens[-1][1]} 超出文档长度 {clen}。")

        conn.execute("UPDATE documents SET is_active=0 WHERE project_id=?", (pid,))
        cur = conn.execute(
            "INSERT INTO documents(project_id,name,content,tokens,is_active,imported_at)"
            " VALUES(?,?,?,?,1,?)",
            (pid, doc_name, file_content, json.dumps(tokens), service.now_iso()),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "name": doc_name,
            "content_length": clen,
            "token_count": len(tokens),
        }
    finally:
        conn.close()


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    conn = get_conn()
    try:
        doc = _row(conn, "SELECT * FROM documents WHERE id=?", (doc_id,))
        d = dict(doc)
        d["tokens"] = json.loads(d["tokens"])
        return d
    finally:
        conn.close()


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM documents WHERE id=?", (doc_id,))
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM adjudications WHERE document_id=?", (doc_id,)
        ).fetchone()["n"]
        if used:
            raise HTTPException(
                409, "该文档已产生裁决/报告，为保证旧报告可追溯，禁止删除。"
            )
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── 标签与关系 ─────────────────────────────────────────────────────────
@app.post("/api/projects/{pid}/labels")
def create_label(pid: int, body: LabelIn):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM projects WHERE id=?", (pid,))
        try:
            cur = conn.execute(
                "INSERT INTO labels(project_id,name,created_at) VALUES(?,?,?)",
                (pid, body.name, service.now_iso()),
            )
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"标签名重复或非法: {body.name}") from exc
        return {"id": cur.lastrowid, "name": body.name}
    finally:
        conn.close()


@app.delete("/api/labels/{label_id}")
def delete_label(label_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM labels WHERE id=?", (label_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/projects/{pid}/relations")
def create_relation(pid: int, body: RelationIn):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM projects WHERE id=?", (pid,))
        labels = {
            r["id"]
            for r in conn.execute("SELECT id FROM labels WHERE project_id=?", (pid,))
        }
        if body.outer_id not in labels or body.inner_id not in labels:
            raise HTTPException(422, "outer_id/inner_id 必须属于当前项目的标签。")
        if body.kind == "nested" and body.outer_id == body.inner_id:
            raise HTTPException(422, "标签不能嵌套自身。")
        try:
            cur = conn.execute(
                "INSERT INTO label_relations(project_id,outer_id,inner_id,kind,created_at)"
                " VALUES(?,?,?,?,?)",
                (pid, body.outer_id, body.inner_id, body.kind, service.now_iso()),
            )
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, "该标签关系已存在。") from exc
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.delete("/api/relations/{rel_id}")
def delete_relation(rel_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM label_relations WHERE id=?", (rel_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── 标注者 ─────────────────────────────────────────────────────────────
@app.post("/api/projects/{pid}/annotators")
def create_annotator(pid: int, body: AnnotatorIn):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM projects WHERE id=?", (pid,))
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM annotators WHERE project_id=?", (pid,)
        ).fetchone()["n"]
        if n >= 8:
            raise HTTPException(422, "每个项目最多 8 名标注者。")
        try:
            cur = conn.execute(
                "INSERT INTO annotators(project_id,name,weight,created_at) VALUES(?,?,?,?)",
                (pid, body.name, body.weight, service.now_iso()),
            )
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"标注者姓名重复: {body.name}") from exc
        return {"id": cur.lastrowid, "name": body.name, "weight": body.weight}
    finally:
        conn.close()


@app.patch("/api/annotators/{aid}")
def update_annotator(aid: int, body: AnnotatorIn):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM annotators WHERE id=?", (aid,))
        conn.execute(
            "UPDATE annotators SET name=?, weight=? WHERE id=?",
            (body.name, body.weight, aid),
        )
        conn.commit()
        return {"id": aid, "name": body.name, "weight": body.weight}
    finally:
        conn.close()


@app.delete("/api/annotators/{aid}")
def delete_annotator(aid: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM annotators WHERE id=?", (aid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── 标注提交 ───────────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/annotations")
def list_annotations(doc_id: int):
    conn = get_conn()
    try:
        _row(conn, "SELECT id FROM documents WHERE id=?", (doc_id,))
        rows = conn.execute(
            """
            SELECT a.id,a.document_id,a.annotator_id,an.name AS annotator_name,
                   a.label_id,l.name AS label_name,a.start_offset,a.end_offset,a.created_at
            FROM annotations a
            JOIN annotators an ON an.id=a.annotator_id
            JOIN labels l ON l.id=a.label_id
            WHERE a.document_id=?
            ORDER BY a.annotator_id,a.start_offset,a.id
            """,
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.put("/api/documents/{doc_id}/annotations")
def submit_annotations(doc_id: int, body: AnnotationSubmission):
    """整体替换某标注者在该文档上的全部标注；越界/切开词元将被拒绝。"""
    conn = get_conn()
    try:
        doc = _row(conn, "SELECT * FROM documents WHERE id=?", (doc_id,))
        annotator = conn.execute(
            "SELECT * FROM annotators WHERE id=? AND project_id=?",
            (body.annotator_id, doc["project_id"]),
        ).fetchone()
        if annotator is None:
            raise HTTPException(422, "标注者不存在或不属于该文档所属项目。")

        label_ids = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM labels WHERE project_id=?", (doc["project_id"],)
            )
        }
        tokens = json.loads(doc["tokens"])
        clen = len(doc["content"])
        errors: list[str] = []
        seen: set[tuple[int, int, int]] = set()
        for i, item in enumerate(body.annotations):
            if item.label_id not in label_ids:
                errors.append(f"第 {i + 1} 条: 标签 id {item.label_id} 不存在。")
                continue
            err = service.validate_span(clen, tokens, item.start, item.end)
            if err:
                errors.append(f"第 {i + 1} 条 [{item.start},{item.end}): {err}")
                continue
            key = (item.label_id, item.start, item.end)
            if key in seen:
                errors.append(
                    f"第 {i + 1} 条与本批次另一条标注重复 [{item.start},{item.end})。"
                )
            seen.add(key)
        if errors:
            return JSONResponse(
                status_code=422,
                content={"detail": "存在非法区间，整批提交已拒绝，未写入任何标注。", "errors": errors},
            )

        conn.execute(
            "DELETE FROM annotations WHERE document_id=? AND annotator_id=?",
            (doc_id, body.annotator_id),
        )
        ts = service.now_iso()
        for item in body.annotations:
            conn.execute(
                "INSERT INTO annotations(document_id,annotator_id,label_id,start_offset,end_offset,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (doc_id, body.annotator_id, item.label_id, item.start, item.end, ts),
            )
        conn.commit()
        return {"ok": True, "accepted": len(body.annotations)}
    finally:
        conn.close()


# ── 裁决 ───────────────────────────────────────────────────────────────
@app.post("/api/documents/{doc_id}/adjudications")
def create_adjudication(doc_id: int):
    conn = get_conn()
    try:
        doc, _project, state = _state_for_doc(conn, doc_id)
        n_annotators = len(state["annotators"])
        if n_annotators < 2 or n_annotators > 8:
            raise HTTPException(422, f"项目需有 2～8 名标注者（当前 {n_annotators} 名）。")
        distinct = {a["annotator_id"] for a in state["annotations"]}
        if len(distinct) < 2:
            raise HTTPException(422, "至少需要 2 名标注者提交标注才能裁决。")

        cur = conn.execute(
            "INSERT INTO adjudications(document_id,result_json,frozen,created_at,updated_at)"
            " VALUES(?,?,0,?,?)",
            (doc_id, "{}", service.now_iso(), service.now_iso()),
        )
        adj_id = cur.lastrowid
        result = _refresh_adj(conn, adj_id)
        return {"id": adj_id, "result": result}
    finally:
        conn.close()


@app.get("/api/adjudications/{adj_id}")
def get_adjudication(adj_id: int):
    conn = get_conn()
    try:
        adj = _row(conn, "SELECT * FROM adjudications WHERE id=?", (adj_id,))
        return {
            "id": adj["id"],
            "document_id": adj["document_id"],
            "frozen": bool(adj["frozen"]),
            "created_at": adj["created_at"],
            "updated_at": adj["updated_at"],
            "result": json.loads(adj["result_json"]),
        }
    finally:
        conn.close()


@app.get("/api/documents/{doc_id}/adjudications")
def list_adjudications(doc_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT a.*, (SELECT MAX(r.version) FROM reports r WHERE r.adjudication_id=a.id) AS report_version
            FROM adjudications a WHERE a.document_id=? ORDER BY a.id
            """,
            (doc_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "frozen": bool(r["frozen"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "report_version": r["report_version"],
            }
            for r in rows
        ]
    finally:
        conn.close()


@app.post("/api/adjudications/{adj_id}/overrides")
def add_override(adj_id: int, body: OverrideIn):
    conn = get_conn()
    try:
        adj = _row(conn, "SELECT * FROM adjudications WHERE id=?", (adj_id,))
        if adj["frozen"]:
            raise HTTPException(409, "裁决已冻结（已生成报告），不可再覆盖。")
        doc = _row(conn, "SELECT * FROM documents WHERE id=?", (adj["document_id"],))
        label = conn.execute(
            "SELECT * FROM labels WHERE id=? AND project_id=?",
            (body.label_id, doc["project_id"]),
        ).fetchone()
        if label is None:
            raise HTTPException(422, "标签不存在或不属于该项目。")
        tokens = json.loads(doc["tokens"])
        err = service.validate_span(len(doc["content"]), tokens, body.start, body.end)
        if err:
            raise HTTPException(422, err)

        conn.execute(
            "DELETE FROM overrides WHERE adjudication_id=? AND label_id=? AND start_offset=? AND end_offset=?",
            (adj_id, body.label_id, body.start, body.end),
        )
        conn.execute(
            "INSERT INTO overrides(adjudication_id,label_id,start_offset,end_offset,action,reason,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (adj_id, body.label_id, body.start, body.end, body.action, body.reason, service.now_iso()),
        )
        conn.commit()
        result = _refresh_adj(conn, adj_id)
        return {"ok": True, "result": result}
    finally:
        conn.close()


@app.delete("/api/adjudications/{adj_id}/overrides/{ov_id}")
def remove_override(adj_id: int, ov_id: int):
    conn = get_conn()
    try:
        adj = _row(conn, "SELECT * FROM adjudications WHERE id=?", (adj_id,))
        if adj["frozen"]:
            raise HTTPException(409, "裁决已冻结（已生成报告），覆盖不可变更。")
        conn.execute("DELETE FROM overrides WHERE id=? AND adjudication_id=?", (ov_id, adj_id))
        conn.commit()
        return {"ok": True, "result": _refresh_adj(conn, adj_id)}
    finally:
        conn.close()


# ── 报告（不可变） ─────────────────────────────────────────────────────
@app.post("/api/adjudications/{adj_id}/reports")
def create_report(adj_id: int):
    conn = get_conn()
    try:
        adj = _row(conn, "SELECT * FROM adjudications WHERE id=?", (adj_id,))
        doc, project, state = _state_for_doc(conn, adj["document_id"])
        result = json.loads(adj["result_json"])
        if result.get("solver_status") == "INFEASIBLE":
            raise HTTPException(409, "当前裁决无解（覆盖约束冲突），不能生成报告。")

        # 版本号在项目内单调递增：重新导入文档/改规则只产生新版本，旧版本永不复用或改写。
        prev = conn.execute(
            "SELECT MAX(version) AS v FROM reports WHERE project_id=?", (project["id"],)
        ).fetchone()["v"]
        version = (prev or 0) + 1

        payload = service.build_report_payload(conn, project, doc, state, dict(adj), result, version)
        cur = conn.execute(
            "INSERT INTO reports(project_id,document_id,adjudication_id,version,payload_json,sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                project["id"],
                doc["id"],
                adj_id,
                version,
                json.dumps(payload, ensure_ascii=False),
                payload["sha256"],
                payload["report"]["generated_at"],
            ),
        )
        conn.execute("UPDATE adjudications SET frozen=1 WHERE id=?", (adj_id,))
        conn.commit()
        return {"id": cur.lastrowid, **payload["report"], "sha256": payload["sha256"]}
    finally:
        conn.close()


@app.get("/api/reports")
def list_reports(project_id: int | None = None):
    conn = get_conn()
    try:
        sql = (
            "SELECT id,project_id,document_id,adjudication_id,version,sha256,created_at,"
            "json_extract(payload_json,'$.report.document_name') AS document_name,"
            "json_extract(payload_json,'$.report.app_version') AS app_version FROM reports"
        )
        args: tuple = ()
        if project_id is not None:
            sql += " WHERE project_id=?"
            args = (project_id,)
        sql += " ORDER BY id"
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


@app.get("/api/reports/{report_id}")
def get_report(report_id: int):
    conn = get_conn()
    try:
        row = _row(conn, "SELECT payload_json FROM reports WHERE id=?", (report_id,))
        return json.loads(row["payload_json"])
    finally:
        conn.close()


@app.get("/api/reports/{report_id}/download")
def download_report(report_id: int):
    conn = get_conn()
    try:
        row = _row(conn, "SELECT payload_json, version FROM reports WHERE id=?", (report_id,))
        blob = service.canonical_json(json.loads(row["payload_json"]))
        return Response(
            content=blob,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="consensus-report-v{row["version"]}.json"'
                )
            },
        )
    finally:
        conn.close()


@app.get("/api/health")
def health():
    import ortools

    from .config import APP_VERSION

    return {"ok": True, "app_version": APP_VERSION, "ortools_version": ortools.__version__}
