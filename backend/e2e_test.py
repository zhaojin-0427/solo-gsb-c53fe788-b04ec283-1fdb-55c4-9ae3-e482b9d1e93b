"""端到端冒烟测试：进程内通过 TestClient 跑完整业务流。

运行：
    python3 backend/e2e_test.py
（使用独立 DATA_DIR=/tmp/adj-test，不影响真实数据）
"""
import json
import os
import shutil
import sys
from pathlib import Path

os.environ["DATA_DIR"] = "/tmp/adj-test"
shutil.rmtree("/tmp/adj-test", ignore_errors=True)
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

CONTENT = "张三在北京大学开会，李四也在。"
TOKENS = [[i, i + 1] for i in range(len(CONTENT))]
failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(name)


with TestClient(app) as c:
    h = c.get("/api/health").json()
    check("健康检查含版本号", h["ok"] and h["app_version"] and h["ortools_version"])

    pid = c.post("/api/projects", json={"name": "测试项目"}).json()["id"]

    # 非 UTF-8 文档被拒
    bad = c.post(
        f"/api/projects/{pid}/documents",
        files={"doc_file": ("bad.txt", b"\xff\xfebad", "application/octet-stream"),
               "tokens_file": ("t.json", json.dumps(TOKENS).encode(), "application/json")},
    )
    check("非 UTF-8 文档被拒绝(422)", bad.status_code == 422)

    r = c.post(
        f"/api/projects/{pid}/documents",
        data={"name": "样本", "content": CONTENT, "tokens_text": json.dumps(TOKENS)},
    ).json()
    doc_id = r["id"]
    check("文档与词元导入", r["content_length"] == len(CONTENT) and r["token_count"] == len(TOKENS))

    l_person = c.post(f"/api/projects/{pid}/labels", json={"name": "Person"}).json()["id"]
    l_org = c.post(f"/api/projects/{pid}/labels", json={"name": "Org"}).json()["id"]
    l_loc = c.post(f"/api/projects/{pid}/labels", json={"name": "Loc"}).json()["id"]
    # 外层 Loc 可包含内层 Org（本测试刻意只在大范围场景生效）
    c.post(f"/api/projects/{pid}/relations",
           json={"outer_id": l_loc, "inner_id": l_org, "kind": "nested"})

    a1 = c.post(f"/api/projects/{pid}/annotators", json={"name": "资深A", "weight": 10}).json()["id"]
    a2 = c.post(f"/api/projects/{pid}/annotators", json={"name": "普通B", "weight": 5}).json()["id"]
    a3 = c.post(f"/api/projects/{pid}/annotators", json={"name": "新手C", "weight": 1}).json()["id"]
    check("权重超范围 11 被拒",
          c.post(f"/api/projects/{pid}/annotators", json={"name": "X", "weight": 11}).status_code == 422)
    check("第 9 名标注者被拒（上限8）", all(
        c.post(f"/api/projects/{pid}/annotators", json={"name": f"u{i}", "weight": 5}).status_code == 200
        for i in range(4, 9)) and
        c.post(f"/api/projects/{pid}/annotators", json={"name": "u9", "weight": 5}).status_code == 422)

    def submit(ann, items, doc=doc_id):
        return c.put(f"/api/documents/{doc}/annotations",
                     json={"annotator_id": ann, "annotations": items})

    # 合法提交（标签关系冲突留给求解器，提交期只验边界）
    r = submit(a1, [
        {"label_id": l_person, "start": 0, "end": 2},
        {"label_id": l_org, "start": 3, "end": 7},
        {"label_id": l_loc, "start": 3, "end": 5},
    ])
    check("合法区间提交成功", r.status_code == 200, r.text)

    r = submit(a2, [
        {"label_id": l_person, "start": 0, "end": 2},
        {"label_id": l_person, "start": 0, "end": 99},
    ])
    check("越界区间整批拒绝", r.status_code == 422 and "越界" in " ".join(r.json().get("errors", [])))

    doc2 = c.post(
        f"/api/projects/{pid}/documents",
        data={"name": "多字词元", "content": "你好世界",
              "tokens_text": json.dumps([[0, 2], [2, 4]])},
    ).json()["id"]
    bad_cut = submit(a2, [{"label_id": l_person, "start": 0, "end": 1}], doc=doc2)
    check("切开词元的区间被拒",
          bad_cut.status_code == 422 and "切开了词元" in " ".join(bad_cut.json().get("errors", [])))

    # ── 三方分歧 ──────────────────────────────────────────────────────
    # A(w10): Person 张三[0,2]; Org 北京大学[3,7]; Loc 北京[3,5]
    submit(a1, [
        {"label_id": l_person, "start": 0, "end": 2},
        {"label_id": l_org, "start": 3, "end": 7},
        {"label_id": l_loc, "start": 3, "end": 5},
    ])
    # B(w5): Person 张[0,1]（与张三 jaccard 1/2）; Person 李四[11,13]; Org[3,7]
    submit(a2, [
        {"label_id": l_person, "start": 0, "end": 1},
        {"label_id": l_person, "start": 11, "end": 13},
        {"label_id": l_org, "start": 3, "end": 7},
    ])
    # C(w1): Person 李四[11,13]; Org 大学[4,7]（与[3,7] jaccard 3/4）
    submit(a3, [
        {"label_id": l_person, "start": 11, "end": 13},
        {"label_id": l_org, "start": 4, "end": 7},
    ])

    adj = c.post(f"/api/documents/{doc_id}/adjudications").json()
    adj_id = adj["id"]
    result = adj["result"]
    check("求解状态 OPTIMAL", result["solver_status"] == "OPTIMAL", str(result.get("infeasible_reason")))
    by_key = {(x["start"], x["end"], x["label_id"]): x for x in result["candidates"]}

    check("Person[0,2] 得分=12.5 (10×1+5×0.5)",
          abs(by_key[(0, 2, l_person)]["score"] - 12.5) < 1e-6,
          str(by_key[(0, 2, l_person)]["score"]))
    check("Person[0,1] 得分=10.0 (5×1+10×0.5)",
          abs(by_key[(0, 1, l_person)]["score"] - 10.0) < 1e-6)
    check("Person[11,13] 得分=6.0 (5+1)",
          abs(by_key[(11, 13, l_person)]["score"] - 6.0) < 1e-6)
    check("Org[3,7] 得分=15.75 (10+5+1×0.75)",
          abs(by_key[(3, 7, l_org)]["score"] - 15.75) < 1e-6,
          str(by_key[(3, 7, l_org)]["score"]))
    check("Org[3,7] 采纳（高分、与短Loc冲突）", by_key[(3, 7, l_org)]["adopted"] is True)
    check("Loc[3,5] 未采纳", by_key[(3, 5, l_loc)]["adopted"] is False)
    check("Person[0,2] 胜过同标签冲突的[0,1]", by_key[(0, 2, l_person)]["adopted"] is True
          and by_key[(0, 1, l_person)]["adopted"] is False)
    check("Person[11,13] 采纳", by_key[(11, 13, l_person)]["adopted"] is True)

    rejected = result["rejected_annotations"]
    li = next((x for x in rejected if (x["start"], x["end"]) == (0, 1)), None)
    check("未采纳标注[0,1)逐条展示", li is not None)
    check("未采纳标注带支持者(资深A, jaccard 0.5)",
          li and any(s["annotator_name"] == "资深A" and s["jaccard"] == 0.5
                     for s in li["supporters"]),
          str(li and li["supporters"]))

    # ── 裁决者覆盖：驳回 Org → Loc 自动上位（此时 C 仅支持李四，Loc 无其他争夺者）──
    # 先临时把 C 的 Org[4,7] 清掉：用一个不冲突的标注替换其整批提交
    submit(a3, [{"label_id": l_person, "start": 11, "end": 13}])
    # 重新求一次裁决（原 adj 已建好，直接覆盖即可；候选集会基于最新标注重算）
    r = c.post(f"/api/adjudications/{adj_id}/overrides", json={
        "label_id": l_org, "start": 3, "end": 7,
        "action": "force_exclude", "reason": "单位名不规范"})
    res2 = r.json()["result"]
    bk2 = {(x["start"], x["end"], x["label_id"]): x for x in res2["candidates"]}
    check("强制驳回 Org 生效", bk2[(3, 7, l_org)]["adopted"] is False
          and bk2[(3, 7, l_org)]["forced"] == "force_exclude")
    check("驳回 Org 后 Loc[3,5] 自动被采纳", bk2[(3, 5, l_loc)]["adopted"] is True,
          str({k: v["adopted"] for k, v in bk2.items()}))
    check("覆盖理由被记录", res2["overrides"][0]["reason"] == "单位名不规范")

    ov_id = res2["overrides"][0]["id"]
    r = c.delete(f"/api/adjudications/{adj_id}/overrides/{ov_id}").json()
    bk3 = {(x["start"], x["end"], x["label_id"]): x for x in r["result"]["candidates"]}
    check("撤销覆盖后恢复自动共识", bk3[(3, 7, l_org)]["adopted"] is True)
    # 恢复 C 的原始提交
    submit(a3, [
        {"label_id": l_person, "start": 11, "end": 13},
        {"label_id": l_org, "start": 4, "end": 7},
    ])

    # 互相矛盾的强制采纳 => INFEASIBLE，可恢复，不出报告
    c.post(f"/api/adjudications/{adj_id}/overrides", json={
        "label_id": l_org, "start": 3, "end": 7, "action": "force_include", "reason": "要"})
    infeas = c.post(f"/api/adjudications/{adj_id}/overrides", json={
        "label_id": l_loc, "start": 3, "end": 5, "action": "force_include", "reason": "也要"})
    check("冲突强制覆盖判为 INFEASIBLE",
          infeas.json()["result"]["solver_status"] == "INFEASIBLE")
    check("无解时禁止生成报告",
          c.post(f"/api/adjudications/{adj_id}/reports").status_code == 409)
    for o in infeas.json()["result"]["overrides"]:
        c.delete(f"/api/adjudications/{adj_id}/overrides/{o['id']}")

    # ── 报告 v1 ───────────────────────────────────────────────────────
    rep1 = c.post(f"/api/adjudications/{adj_id}/reports").json()
    rid1 = rep1["id"]
    check("首份报告版本号 v1", rep1["report_version"] == 1)
    full1 = c.get(f"/api/reports/{rid1}").json()
    check("报告含 SHA-256", len(full1["sha256"]) == 64)
    check("报告含输入快照(原文)", full1["input_snapshot"]["document"]["content"] == CONTENT)
    check("报告含算法参数(固定种子)",
          full1["algorithm"]["solver_params"]["random_seed"] == 20261001)
    check("报告含应用/求解器版本",
          bool(full1["report"]["app_version"]) and bool(full1["report"]["ortools_version"]))

    check("冻结后禁止再加覆盖", c.post(f"/api/adjudications/{adj_id}/overrides", json={
        "label_id": l_person, "start": 0, "end": 2,
        "action": "force_exclude", "reason": "x"}).status_code == 409)

    # ── 改规则/权重/重新导入，旧报告不得变 ────────────────────────────
    c.patch(f"/api/annotators/{a3}", json={"name": "新手C", "weight": 10})
    c.post(f"/api/projects/{pid}/labels", json={"name": "NewTag"})
    c.post(
        f"/api/projects/{pid}/documents",
        data={"name": "样本-重导", "content": CONTENT + "追加",
              "tokens_text": json.dumps([[i, i + 1] for i in range(len(CONTENT) + 2)])},
    )
    again = c.get(f"/api/reports/{rid1}").json()
    check("改权重/加标签/重新导入后旧报告字节级不变", again == full1)
    check("旧报告 SHA-256 不变", again["sha256"] == full1["sha256"])

    # ── 新裁决新版本 v2；v1 仍可访问 ──────────────────────────────────
    new_doc = c.get(f"/api/projects/{pid}").json()["documents"][-1]["id"]
    submit(a1, [{"label_id": l_person, "start": 0, "end": 2}], doc=new_doc)
    submit(a2, [{"label_id": l_person, "start": 0, "end": 2}], doc=new_doc)
    adj2 = c.post(f"/api/documents/{new_doc}/adjudications").json()["id"]
    rep2 = c.post(f"/api/adjudications/{adj2}/reports").json()
    check("第二次生成得到 v2", rep2["report_version"] == 2)
    check("v1 报告仍可访问", c.get(f"/api/reports/{rid1}").status_code == 200)
    check("报告列表含 2 份",
          len(c.get(f"/api/reports?project_id={pid}").json()) == 2)

    # ── 确定性：两次独立裁决结果一致 ──────────────────────────────────
    x = c.post(f"/api/documents/{doc_id}/adjudications").json()["result"]["candidates"]
    y = c.post(f"/api/documents/{doc_id}/adjudications").json()["result"]["candidates"]
    key = lambda cs: sorted((z["start"], z["end"], z["label_id"], z["adopted"], z["score"]) for z in cs)
    check("同输入两次裁决结果一致(确定性)", key(x) == key(y))

    # ── 平局序：等权同标签部分重叠 => 起点靠前的候选优先采纳 ───────────
    pid2 = c.post("/api/projects", json={"name": "平局测试"}).json()["id"]
    d = c.post(f"/api/projects/{pid2}/documents", data={
        "content": "abcdef", "tokens_text": json.dumps([[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6]])},
    ).json()["id"]
    t = c.post(f"/api/projects/{pid2}/labels", json={"name": "T"}).json()["id"]
    u1 = c.post(f"/api/projects/{pid2}/annotators", json={"name": "u1", "weight": 5}).json()["id"]
    u2 = c.post(f"/api/projects/{pid2}/annotators", json={"name": "u2", "weight": 5}).json()["id"]
    # u1 独占 [0,2]；u2 独占 [1,3]：得分各 5、互相冲突 => 起点小的 [0,2] 胜
    c.put(f"/api/documents/{d}/annotations",
          json={"annotator_id": u1, "annotations": [{"label_id": t, "start": 0, "end": 2}]}).raise_for_status()
    c.put(f"/api/documents/{d}/annotations",
          json={"annotator_id": u2, "annotations": [{"label_id": t, "start": 1, "end": 3}]}).raise_for_status()
    tb = c.post(f"/api/documents/{d}/adjudications").json()["result"]
    tbk = {(x["start"], x["end"]): x for x in tb["candidates"]}
    check("平分时按区间起点优先采纳[0,2]",
          tbk[(0, 2)]["adopted"] and not tbk[(1, 3)]["adopted"], str(tbk))

    # ── 下载 ──────────────────────────────────────────────────────────
    dl = c.get(f"/api/reports/{rid1}/download")
    check("报告下载端点返回 JSON 且哈希一致",
          dl.status_code == 200 and "application/json" in dl.headers["content-type"]
          and json.loads(dl.content)["sha256"] == full1["sha256"])

print()
if failures:
    print(f"{len(failures)} 项失败：")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL E2E ASSERTIONS PASSED")
