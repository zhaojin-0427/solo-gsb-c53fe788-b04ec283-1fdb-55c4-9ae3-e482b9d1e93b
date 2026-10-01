"""共识求解核心。

候选：所有输入标注按 (start, end, label) 去重得到候选区间。
候选得分：每名标注者对该 (区间,标签) 的贡献 = 权重 × 与其同标签、
相交标注的最大 Jaccard 重叠比例（完全重合=1，故同一标注者至多贡献其权重）。
最优共识：在嵌套/互斥约束下使总得分最大的候选集合（CP-SAT 整数规划）。
确定性平局：最优解不唯一时，按 (起点, 终点, 标签名, 标签id) 升序逐个固定
候选取舍（靠前的区间优先采纳），结果与求解器内部搜索顺序无关。
"""
from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from .config import SCORE_SCALE


@dataclass(frozen=True)
class CandidateKey:
    start: int
    end: int
    label_id: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.start, self.end, self.label_id)


def jaccard(a: tuple[int, int], b: tuple[int, int]) -> float:
    s1, e1 = a
    s2, e2 = b
    inter = max(0, min(e1, e2) - max(s1, s2))
    if inter == 0:
        return 0.0
    union = max(e1, e2) - min(s1, s2)
    return inter / union if union else 0.0


def spans_conflict(
    c1: CandidateKey,
    c2: CandidateKey,
    nested_pairs: set[tuple[int, int]],
) -> bool:
    """两个候选是否不能同时进入共识。"""
    if c1.start == c2.start and c1.end == c2.end and c1.label_id == c2.label_id:
        return False  # 去重后的同一候选
    overlap = min(c1.end, c2.end) - max(c1.start, c2.start)
    if overlap <= 0:
        return False
    if c1.label_id == c2.label_id:
        return True  # 同标签重叠恒冲突

    def inside(a: CandidateKey, b: CandidateKey) -> bool:
        # b 严格（允许共享边界、长度须更小）包含于 a：可嵌套
        return (
            a.start <= b.start
            and b.end <= a.end
            and (a.end - a.start) > (b.end - b.start)
        )

    if inside(c1, c2) and (c1.label_id, c2.label_id) in nested_pairs:
        return False
    if inside(c2, c1) and (c2.label_id, c1.label_id) in nested_pairs:
        return False
    # 未声明嵌套（含显式互斥、交叠但不包含、同起止跨标签）一律冲突。
    return True


def _solve(model: cp_model.CpModel, params: dict) -> tuple[int, cp_model.CpSolver]:
    solver = cp_model.CpSolver()
    for key, value in params.items():
        setattr(solver.parameters, key, value)
    return solver.Solve(model), solver


def solve_consensus(
    candidates: list[dict],
    nested_pairs: set[tuple[int, int]],
    overrides: dict[tuple[int, int, int], dict],
    params: dict,
) -> dict:
    """求最优共识。

    candidates: [{key: CandidateKey, score_int: int, ...展示字段}]，已按平局序排好。
    overrides: {(start,end,label_id): {"action": ..., "reason": ...}}
    返回 {"adopted": set[key], "objective": int, "status": str,
          "infeasible_reason": str|None}
    """
    keys = [c["key"] for c in candidates]
    # overrides 的键为 (start,end,label_id) 元组；统一转成 CandidateKey 对象。
    override_by_key = {
        CandidateKey(s, e, lid): ov for (s, e, lid), ov in overrides.items()
    }

    def build_model() -> tuple[cp_model.CpModel, dict]:
        model = cp_model.CpModel()
        x = {k: model.NewBoolVar(f"c_{i}") for i, k in enumerate(keys)}
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                if spans_conflict(a, b, nested_pairs):
                    model.Add(x[a] + x[b] <= 1)
        for k, ov in override_by_key.items():
            if k not in x:
                continue  # 针对已消失候选的覆盖不参与建模
            model.Add(x[k] == (1 if ov["action"] == "force_include" else 0))
        return model, x

    # ── 第一阶段：可行性（覆盖可能互相矛盾）────────────────────────────
    model, x = build_model()
    status, solver = _solve(model, params)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "adopted": set(),
            "objective": 0,
            "status": "INFEASIBLE",
            "infeasible_reason": "裁决者的强制采纳/驳回约束互相冲突（或违反标签嵌套/互斥规则），无解。",
        }

    # ── 第二阶段：最大化总得分 ────────────────────────────────────────
    model, x = build_model()
    model.Maximize(sum(c["score_int"] * x[c["key"]] for c in candidates))
    status, solver = _solve(model, params)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "adopted": set(),
            "objective": 0,
            "status": "INFEASIBLE",
            "infeasible_reason": "求解最优共识失败。",
        }
    best = int(solver.ObjectiveValue())

    # ── 第三阶段：固定最优总分，按平局序逐个确定候选（确定性）──────────
    fixed: dict[CandidateKey, int] = {}
    for k, ov in override_by_key.items():
        if k in keys:
            fixed[k] = 1 if ov["action"] == "force_include" else 0

    for k in keys:
        if k in fixed:
            continue
        probe = cp_model.CpModel()
        y = {kk: probe.NewBoolVar(f"c_{i}") for i, kk in enumerate(keys)}
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                if spans_conflict(a, b, nested_pairs):
                    probe.Add(y[a] + y[b] <= 1)
        probe.Add(sum(c["score_int"] * y[c["key"]] for c in candidates) == best)
        for fk, fv in fixed.items():
            probe.Add(y[fk] == fv)
        probe.Add(y[k] == 1)  # 平局序靠前的区间优先采纳
        st, _ = _solve(probe, params)
        fixed[k] = 1 if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0

    return {
        "adopted": {k for k, v in fixed.items() if v == 1},
        "objective": best,
        "status": "OPTIMAL",
        "infeasible_reason": None,
    }
