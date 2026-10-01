"""裁决业务逻辑：区间校验、候选与得分构建、报告快照组装。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .config import APP_NAME, APP_VERSION, SCORE_SCALE, SOLVER_PARAMS
from .solver import CandidateKey, jaccard, solve_consensus


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_span(content_len: int, tokens: list[tuple[int, int]], start: int, end: int) -> str | None:
    """返回错误信息；None 表示合法。半开区间 [start, end)。"""
    if start < 0 or end <= start or end > content_len:
        return f"区间 [{start},{end}) 越界（文档长度 {content_len}）"
    covered = False
    for ts, te in tokens:
        if te <= start:
            continue
        if ts >= end:
            break
        # 与区间相交的词元必须被完整包含，否则就是“切开词元”。
        if ts < start or te > end:
            return f"区间 [{start},{end}) 切开了词元 [{ts},{te})"
        covered = True
    if not covered:
        return f"区间 [{start},{end}) 未完整覆盖任何词元"
    return None


def load_document_state(conn, doc: dict) -> dict:
    labels = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM labels WHERE project_id=? ORDER BY id", (doc["project_id"],)
        )
    }
    rel_rows = conn.execute(
        "SELECT * FROM label_relations WHERE project_id=?", (doc["project_id"],)
    ).fetchall()
    nested_pairs = {
        (r["outer_id"], r["inner_id"]) for r in rel_rows if r["kind"] == "nested"
    }
    annotators = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM annotators WHERE project_id=? ORDER BY id",
            (doc["project_id"],),
        )
    }
    ann_rows = conn.execute(
        """
        SELECT a.*, an.name AS annotator_name, an.weight AS annotator_weight,
               l.name AS label_name
        FROM annotations a
        JOIN annotators an ON an.id = a.annotator_id
        JOIN labels l ON l.id = a.label_id
        WHERE a.document_id=?
        ORDER BY a.id
        """,
        (doc["id"],),
    ).fetchall()
    return {
        "labels": labels,
        "relations": [dict(r) for r in rel_rows],
        "nested_pairs": nested_pairs,
        "annotators": annotators,
        "annotations": [dict(r) for r in ann_rows],
    }


def build_candidates(state: dict) -> list[dict]:
    """由输入标注去重生成候选，并计算每名标注者的重叠贡献。"""
    anns = state["annotations"]
    labels = state["labels"]
    raw: dict[tuple[int, int, int], dict] = {}
    for a in anns:
        key = CandidateKey(a["start_offset"], a["end_offset"], a["label_id"])
        raw.setdefault(
            key.as_tuple(),
            {"key": key, "start": key.start, "end": key.end, "label_id": key.label_id},
        )

    by_label: dict[int, list[dict]] = {}
    for a in anns:
        by_label.setdefault(a["label_id"], []).append(a)

    candidates: list[dict] = []
    for cand in raw.values():
        s, e, lid = cand["start"], cand["end"], cand["label_id"]
        per_annotator_best: dict[int, tuple[float, dict]] = {}
        for a in by_label.get(lid, []):
            if a["end_offset"] <= s or a["start_offset"] >= e:
                continue  # 仅统计相交标注
            j = jaccard((s, e), (a["start_offset"], a["end_offset"]))
            prev = per_annotator_best.get(a["annotator_id"])
            if prev is None or j > prev[0]:
                per_annotator_best[a["annotator_id"]] = (j, a)
        supporters = []
        score = 0.0
        for (j, a) in sorted(per_annotator_best.values(), key=lambda t: -t[0]):
            if j <= 0:
                continue
            supporters.append(
                {
                    "annotation_id": a["id"],
                    "annotator_id": a["annotator_id"],
                    "annotator_name": a["annotator_name"],
                    "weight": a["annotator_weight"],
                    "jaccard": round(j, 6),
                }
            )
            score += a["annotator_weight"] * j
        score_int = round(score * SCORE_SCALE)
        candidates.append(
            {
                **cand,
                "label_name": labels[lid]["name"],
                "score": round(score, 6),
                "score_int": score_int,
                "supporters": supporters,
            }
        )

    # 平局裁决顺序：起点、终点、标签名、标签 id。
    candidates.sort(key=lambda c: (c["start"], c["end"], c["label_name"], c["label_id"]))
    return candidates


def recompute(conn, doc: dict, state: dict, overrides_rows: list[dict]) -> dict:
    """执行求解并返回可存储/展示的结果对象。"""
    import ortools

    candidates = build_candidates(state)
    override_map = {
        (o["start_offset"], o["end_offset"], o["label_id"]): {
            "action": o["action"],
            "reason": o["reason"],
        }
        for o in overrides_rows
    }
    sol = solve_consensus(
        candidates,
        state["nested_pairs"],
        override_map,
        SOLVER_PARAMS,
    )
    adopted = sol["adopted"]

    out_candidates = []
    for c in candidates:
        k = c["key"]
        forced = override_map.get(k.as_tuple())
        out_candidates.append(
            {
                "start": c["start"],
                "end": c["end"],
                "label_id": c["label_id"],
                "label_name": c["label_name"],
                "score": c["score"],
                "score_int": c["score_int"],
                "adopted": k in adopted,
                "forced": (forced or {}).get("action"),
                "supporters": c["supporters"],
            }
        )

    # 未采纳标注：精确 (start,end,label) 候选未入选的每条输入标注。
    adopted_keys = {k.as_tuple() for k in adopted}
    by_label: dict[int, list[dict]] = {}
    for a in state["annotations"]:
        by_label.setdefault(a["label_id"], []).append(a)
    rejected = []
    for a in state["annotations"]:
        tup = (a["start_offset"], a["end_offset"], a["label_id"])
        if tup in adopted_keys:
            continue
        supporters = []
        best_j = 0.0
        for b in by_label.get(a["label_id"], []):
            if b["annotator_id"] == a["annotator_id"]:
                continue
            if b["end_offset"] <= a["start_offset"] or b["start_offset"] >= a["end_offset"]:
                continue
            j = jaccard(
                (a["start_offset"], a["end_offset"]),
                (b["start_offset"], b["end_offset"]),
            )
            best_j = max(best_j, j)
            supporters.append(
                {
                    "annotation_id": b["id"],
                    "annotator_id": b["annotator_id"],
                    "annotator_name": b["annotator_name"],
                    "weight": b["annotator_weight"],
                    "jaccard": round(j, 6),
                }
            )
        supporters.sort(key=lambda s: (-s["jaccard"], s["annotator_id"]))
        rejected.append(
            {
                "annotation_id": a["id"],
                "annotator_id": a["annotator_id"],
                "annotator_name": a["annotator_name"],
                "label_id": a["label_id"],
                "label_name": a["label_name"],
                "start": a["start_offset"],
                "end": a["end_offset"],
                "same_label_max_jaccard": round(best_j, 6),
                "supporters": supporters,
            }
        )

    return {
        "solver_status": sol["status"],
        "infeasible_reason": sol["infeasible_reason"],
        "objective_int": sol["objective"],
        "objective": round(sol["objective"] / SCORE_SCALE, 6),
        "overrides": [
            {
                "id": o["id"],
                "label_id": o["label_id"],
                "start": o["start_offset"],
                "end": o["end_offset"],
                "action": o["action"],
                "reason": o["reason"],
            }
            for o in overrides_rows
        ],
        "candidates": out_candidates,
        "rejected_annotations": rejected,
        "ortools_version": ortools.__version__,
    }


def build_report_payload(
    conn, project: dict, doc: dict, state: dict, adj: dict, result: dict, version: int
) -> dict:
    """组装完整报告负载（含输入快照、算法参数、版本号），并计算 SHA-256。"""
    tokens = json.loads(doc["tokens"])
    payload = {
        "report": {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "ortools_version": result["ortools_version"],
            "report_version": version,
            "project_id": project["id"],
            "project_name": project["name"],
            "document_id": doc["id"],
            "document_name": doc["name"],
            "adjudication_id": adj["id"],
            "generated_at": now_iso(),
            "immutable": True,
        },
        "input_snapshot": {
            "document": {
                "id": doc["id"],
                "name": doc["name"],
                "content": doc["content"],
                "tokens": tokens,
            },
            "labels": [
                {"id": lid, "name": l["name"]}
                for lid, l in sorted(state["labels"].items())
            ],
            "label_relations": [
                {
                    "outer_label_id": r["outer_id"],
                    "outer_label_name": state["labels"][r["outer_id"]]["name"],
                    "inner_label_id": r["inner_id"],
                    "inner_label_name": state["labels"][r["inner_id"]]["name"],
                    "kind": r["kind"],
                }
                for r in sorted(state["relations"], key=lambda r: r["id"])
            ],
            "annotators": [
                {"id": aid, "name": a["name"], "weight": a["weight"]}
                for aid, a in sorted(state["annotators"].items())
            ],
            "annotations": [
                {
                    "id": a["id"],
                    "annotator_id": a["annotator_id"],
                    "annotator_name": a["annotator_name"],
                    "weight": a["annotator_weight"],
                    "label_id": a["label_id"],
                    "label_name": a["label_name"],
                    "start": a["start_offset"],
                    "end": a["end_offset"],
                }
                for a in state["annotations"]
            ],
        },
        "algorithm": {
            "scoring": (
                "候选=输入标注按(起点,终点,标签)去重；"
                "候选得分=Σ_标注者 权重×max Jaccard(候选, 该标注者同标签相交标注)，"
                "同一标注者多条重叠标注只取最大重叠、不重复计权。"
            ),
            "optimization": "在标签嵌套/互斥约束下，用 OR-Tools CP-SAT 求总得分最大的候选集合（整数目标）。",
            "constraints": (
                "同标签区间重叠恒冲突；不同标签默认互斥，"
                "仅显式 nested 关系允许外层严格包含内层；未声明即不可重叠。"
            ),
            "tie_breaker": "总分相等时按(区间起点,区间终点,标签名,标签id)升序依次优先采纳，保证结果确定。",
            "score_scale": SCORE_SCALE,
            "solver_params": SOLVER_PARAMS,
        },
        "result": result,
    }
    blob = canonical_json(payload)
    payload["sha256"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return payload


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
