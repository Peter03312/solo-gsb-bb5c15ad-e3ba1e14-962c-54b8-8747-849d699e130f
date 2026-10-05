"""双段结果质量筛选接口测试。

覆盖：
- 按 seg2_covered/total 的路径级筛选（mapped/partial 整行、歧义按路径）；
- 歧义行保留通过阈值的全部候选，不自动裁决；全部路径不过则整行丢弃；
- unmapped 始终统计但不写入筛选 TSV；
- 阈值越界 / 参数格式错误 / 缺少参数 / 单段结果 / token 失效的错误返回；
- 筛选只读：不改变原任务的原始 TSV、裁决版本与已有裁决；
- 下载 TSV 与原始双段 TSV 同表头（25 列）。
"""

import io
import os

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app

client = TestClient(app)

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")

ALL_STATUSES = [("statuses", "mapped"), ("statuses", "partial"),
                ("statuses", "ambiguous")]


def _post2(bed_bytes: bytes | None = None, chain1: bytes | None = None,
           chain2: bytes | None = None):
    if bed_bytes is None:
        paths = ("example_double.bed", "demo_seg1.chain", "demo_seg2.chain")
        with open(os.path.join(EXAMPLES, paths[0]), "rb") as bf, \
             open(os.path.join(EXAMPLES, paths[1]), "rb") as c1, \
             open(os.path.join(EXAMPLES, paths[2]), "rb") as c2:
            return _post2(bf.read(), c1.read(), c2.read())
    return client.post(
        "/api/liftover2",
        files={
            "bed": ("b.bed", io.BytesIO(bed_bytes), "text/plain"),
            "chain1": ("c1.chain", io.BytesIO(chain1), "text/plain"),
            "chain2": ("c2.chain", io.BytesIO(chain2), "text/plain"),
        },
        data={"side1": "target", "side2": "target"},
    )


@pytest.fixture()
def job2():
    resp = _post2()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["download_token"], data["preview"]["rows"]


def _filter(token, min_coverage, statuses=("mapped", "partial", "ambiguous")):
    params = [("min_coverage", str(min_coverage))]
    params += [("statuses", s) for s in statuses]
    return client.get(f"/api/result/{token}/quality-filter", params=params)


def _rows_by_line(rows):
    return {r["line_no"]: r for r in rows}


def _tsv_lines(resp):
    return resp.content.decode("utf-8-sig").splitlines()


# ---------- 预览：阈值与状态勾选 ----------

def test_filter_keeps_full_coverage_and_counts_unmapped(job2):
    token, _ = job2
    resp = _filter(token, 1.0)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    # 行3 mapped(100%) 与行6 歧义(两条路径都 100%) 通过；partial 行被阈值丢弃
    assert [r["line_no"] for r in d["rows"]] == [3, 6]
    c = d["counts"]
    assert c["total"] == 2
    assert c["kept"] == {"mapped": 1, "partial": 0, "ambiguous": 1, "unmapped": 0}
    # 原任务全量统计始终包含 unmapped
    assert c["source_total"] == 8
    assert c["source"] == {"mapped": 1, "partial": 2, "ambiguous": 1, "unmapped": 4}
    assert c["dropped_unmapped"] == 4
    assert c["dropped_by_threshold"] == 2
    assert d["min_coverage"] == 1.0
    assert set(d["statuses"]) == {"mapped", "partial", "ambiguous"}


def test_filter_threshold_partial_rows(job2):
    token, _ = job2
    # 行4 = 100/120 ≈ 0.833；行5 = 10/60 ≈ 0.167
    d = _filter(token, 0.5).json()
    assert [r["line_no"] for r in d["rows"]] == [3, 4, 6]
    assert d["counts"]["kept"]["partial"] == 1

    d = _filter(token, 0.0).json()
    # 阈值 0：所有有路径的行都保留（unmapped 仍不写入）
    assert [r["line_no"] for r in d["rows"]] == [3, 4, 5, 6]
    assert d["counts"]["total"] == 4
    assert d["counts"]["dropped_total"] == 4  # 仅 4 个 unmapped
    assert d["counts"]["dropped_unmapped"] == 4


def test_filter_statuses_selection(job2):
    token, _ = job2
    d = _filter(token, 0.0, statuses=("mapped",)).json()
    assert [r["line_no"] for r in d["rows"]] == [3]
    assert d["counts"]["dropped_by_status"] == 3  # 2 partial + 1 ambiguous
    assert d["counts"]["dropped_by_threshold"] == 0

    d = _filter(token, 1.0, statuses=("ambiguous",)).json()
    assert [r["line_no"] for r in d["rows"]] == [6]

    d = _filter(token, 0.0, statuses=("partial", "ambiguous")).json()
    assert [r["line_no"] for r in d["rows"]] == [4, 5, 6]


def test_filter_preview_row_carries_path_coverage(job2):
    token, _ = job2
    d = _filter(token, 0.0).json()
    by_line = _rows_by_line(d["rows"])
    p4 = by_line[4]["candidates"][0]
    assert p4["seg2_covered"] == 100 and p4["total"] == 120
    assert p4["coverage"] == round(100 / 120, 6)
    p6_paths = by_line[6]["candidates"]
    assert {p["path_index"] for p in p6_paths} == {1, 2}
    assert all(p["coverage"] == 1.0 for p in p6_paths)


# ---------- 歧义行按路径分别判断 ----------

# 构造数据：第一段 chr1:0-100 唯一命中 → 中间 chrM:0-100；
# 第二段 chrM 上两条 chain（id 21 全覆盖 100bp、id 22 只覆盖前 60bp），
# 形成两条覆盖率不同的歧义路径。
_BED_DIFF = b"chr1\t0\t100\n"
_CHAIN1_DIFF = (
    b"chain 1000 chr1 500 + 0 100 chrM 500 + 0 100 1\n100\n"
)
_CHAIN2_DIFF = (
    b"chain 500 chrM 500 + 0 100 chrX 500 + 0 100 21\n100\n"
    b"chain 500 chrM 500 + 0 60 chrY 500 + 0 60 22\n60\n"
)


def test_ambiguous_judged_per_path_keeps_all_passing_candidates():
    resp = _post2(_BED_DIFF, _CHAIN1_DIFF, _CHAIN2_DIFF)
    assert resp.status_code == 200, resp.text
    token = resp.json()["download_token"]

    # 阈值 0.5：两条路径（100% 与 60%）都通过，全部保留，不替用户裁决
    d = _filter(token, 0.5).json()
    assert d["counts"]["kept"]["ambiguous"] == 1
    row = d["rows"][0]
    assert row["status"] == "ambiguous"
    assert [p["path_index"] for p in row["candidates"]] == [1, 2]
    assert [p["chain2_id"] for p in row["candidates"]] == ["21", "22"]
    assert d["counts"]["ambiguous_paths_dropped"] == 0

    # 阈值 0.8：只保留路径 1（100%），路径 2（60%）被裁掉；
    # 歧义行仍然保留（不自动裁决，不把行改判成 mapped）
    d = _filter(token, 0.8).json()
    assert d["counts"]["kept"]["ambiguous"] == 1
    assert d["counts"]["ambiguous_paths_dropped"] == 1
    row = d["rows"][0]
    assert row["status"] == "ambiguous"
    assert len(row["candidates"]) == 1
    assert row["candidates"][0]["path_index"] == 1
    assert row["candidates"][0]["chain2_id"] == "21"


# 两条路径都低于阈值（40%、30%）时整行丢弃
_CHAIN2_BOTH_LOW = (
    b"chain 500 chrM 500 + 0 40 chrX 500 + 0 40 21\n40\n"
    b"chain 500 chrM 500 + 0 30 chrY 500 + 0 30 22\n30\n"
)


def test_ambiguous_row_dropped_when_all_paths_below_threshold():
    resp = _post2(_BED_DIFF, _CHAIN1_DIFF, _CHAIN2_BOTH_LOW)
    assert resp.status_code == 200, resp.text
    token = resp.json()["download_token"]
    # 原始结果仍是 1 行歧义（两条路径）
    assert resp_preview_amb_count(token) == 1

    d = _filter(token, 0.5).json()
    assert d["rows"] == []
    assert d["counts"]["kept"]["ambiguous"] == 0
    assert d["counts"]["dropped"]["ambiguous"] == 1
    assert d["counts"]["dropped_by_threshold"] == 1
    assert d["counts"]["ambiguous_paths_dropped"] == 2
    assert d["counts"]["total"] == 0


def resp_preview_amb_count(token):
    d = client.get(f"/api/result/{token}/decisions").json()
    return len(d["decisions"])


# ---------- 筛选 TSV 下载 ----------

def test_filtered_download_same_header_and_no_unmapped(job2):
    token, _ = job2
    resp = client.get(
        f"/api/download/{token}/filtered",
        params=[("min_coverage", "1.0")] + ALL_STATUSES,
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert ".filtered." in resp.headers["content-disposition"]
    lines = _tsv_lines(resp)
    orig_header = _tsv_lines(client.get(f"/api/download/{token}"))[0].split("\t")
    assert lines[0].split("\t") == orig_header       # 25 列与原始一致
    assert len(lines[0].split("\t")) == 25
    body = lines[1:]
    # 只有行 3（mapped）与行 6 的两条路径；无 unmapped、无 partial
    assert {ln.split("\t")[0] for ln in body} == {"3", "6"}
    assert not any("\tunmapped\t" in ln for ln in body)
    assert not any("\tpartial\t" in ln for ln in body)
    assert len([ln for ln in body if ln.startswith("6\t")]) == 2


def test_filtered_download_ambiguous_keeps_only_passing_path():
    resp = _post2(_BED_DIFF, _CHAIN1_DIFF, _CHAIN2_DIFF)
    token = resp.json()["download_token"]
    dl = client.get(
        f"/api/download/{token}/filtered",
        params=[("min_coverage", "0.8"), ("statuses", "ambiguous")],
    )
    assert dl.status_code == 200
    lines = _tsv_lines(dl)
    body = [ln.split("\t") for ln in lines[1:]]
    assert len(body) == 1
    # path_index 沿用原始序号 1，status 仍是 ambiguous（未裁决）
    assert body[0][1] == "ambiguous" and body[0][6] == "1"
    assert body[0][11] == "chrX"


# ---------- 错误：阈值越界 / 参数格式 / 缺参数 / 单段 / token ----------

@pytest.mark.parametrize("bad", ["-0.01", "1.01", "2", "abc", "", "nan", "inf", "-inf"])
def test_filter_invalid_min_coverage_rejected(job2, bad):
    token, _ = job2
    resp = client.get(
        f"/api/result/{token}/quality-filter",
        params=[("min_coverage", bad)] + ALL_STATUSES,
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]


def test_filter_missing_params(job2):
    token, _ = job2
    r1 = client.get(f"/api/result/{token}/quality-filter", params=ALL_STATUSES)
    assert r1.status_code == 400 and any("min_coverage" in e for e in r1.json()["errors"])
    r2 = client.get(f"/api/result/{token}/quality-filter",
                    params={"min_coverage": "0.5"})
    assert r2.status_code == 400 and any("statuses" in e for e in r2.json()["errors"])


@pytest.mark.parametrize("bad_status", ["unmapped", "Mapped", "bogus", ""])
def test_filter_invalid_status_rejected(job2, bad_status):
    token, _ = job2
    resp = client.get(
        f"/api/result/{token}/quality-filter",
        params=[("min_coverage", "0.5"), ("statuses", bad_status)],
    )
    assert resp.status_code == 400
    assert any("statuses" in e for e in resp.json()["errors"])


def test_filter_unknown_token_404():
    resp = client.get("/api/result/not-a-token/quality-filter",
                      params={"min_coverage": "0.5", "statuses": "mapped"})
    assert resp.status_code == 404
    assert resp.json()["errors"]


def test_filter_rejected_for_single_segment_job():
    with open(os.path.join(EXAMPLES, "example_target.bed"), "rb") as bf, \
         open(os.path.join(EXAMPLES, "demo.chain"), "rb") as cf:
        resp = client.post(
            "/api/liftover",
            files={"bed": ("b.bed", bf, "text/plain"),
                   "chain": ("c.chain", cf, "text/plain")},
            data={"side": "target"},
        )
    token = resp.json()["download_token"]
    r = client.get(f"/api/result/{token}/quality-filter",
                   params={"min_coverage": "0.5", "statuses": "mapped"})
    assert r.status_code == 400
    assert any("双段" in e for e in r.json()["errors"])
    r = client.get(f"/api/download/{token}/filtered",
                   params={"min_coverage": "0.5", "statuses": "mapped"})
    assert r.status_code == 400


def test_filter_errors_do_not_change_original_job(job2):
    token, rows = job2
    before = client.get(f"/api/download/{token}").content
    n_jobs = len(main_module._jobs)
    for params in (
        [("min_coverage", "1.5"), ("statuses", "mapped")],
        [("min_coverage", "0.5"), ("statuses", "bogus")],
    ):
        r = client.get(f"/api/result/{token}/quality-filter", params=params)
        assert r.status_code == 400
    # 原始下载字节不变；任务数量不增加；裁决版本不变
    assert client.get(f"/api/download/{token}").content == before
    assert len(main_module._jobs) == n_jobs
    assert client.get(f"/api/result/{token}/decisions").json()["version"] == 0


# ---------- 筛选与原任务/裁决互不影响 ----------

def test_filter_does_not_affect_decisions_or_original(job2):
    token, rows = job2
    line = next(r["line_no"] for r in rows if r["status"] == "ambiguous")
    before = client.get(f"/api/download/{token}").content

    assert _filter(token, 0.9).status_code == 200
    # 裁决版本不受筛选影响
    assert client.get(f"/api/result/{token}/decisions").json()["version"] == 0

    # 做一次裁决后再筛选：筛选结果仍输出全部通过路径（不读取裁决），
    # 且原始/裁决版下载不受筛选影响。
    d = client.post(
        f"/api/result/{token}/decisions",
        json={"version": 0, "source_line": line, "candidate_index": 2},
    )
    assert d.status_code == 200
    fd = _filter(token, 1.0).json()
    row6 = _rows_by_line(fd["rows"])[line]
    assert len(row6["candidates"]) == 2  # 已裁决不影响筛选：两条 100% 路径都保留

    adj = _tsv_lines(client.get(f"/api/download/{token}/adjudicated"))
    amb = [ln for ln in adj[1:] if ln.startswith(f"{line}\tambiguous\t")]
    assert len(amb) == 1 and amb[0].endswith("\t已裁决")
    assert client.get(f"/api/download/{token}").content == before


def test_filtered_download_also_validated(job2):
    token, _ = job2
    r = client.get(f"/api/download/{token}/filtered",
                   params=[("min_coverage", "9"), ("statuses", "mapped")])
    assert r.status_code == 400
    r = client.get("/api/download/missing/filtered",
                   params={"min_coverage": "0.5", "statuses": "mapped"})
    assert r.status_code == 404


def test_existing_endpoints_still_compatible(job2):
    """原转换/下载/裁决接口的行为与字段不因筛选功能改变。"""
    token, rows = job2
    preview = client.get("/healthz")
    assert preview.status_code == 200
    dl = client.get(f"/api/download/{token}")
    assert dl.status_code == 200
    assert len(_tsv_lines(dl)[0].split("\t")) == 25
