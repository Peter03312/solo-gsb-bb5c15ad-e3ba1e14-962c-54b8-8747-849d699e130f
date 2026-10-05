"""双段结果的质量筛选（按路径最终覆盖率）。

在**已有双段迁移结果**上做只读筛选，不改动原任务的任何内容（原始
TSV、裁决状态均不受影响）：

- 用户输入最小最终覆盖率 ``min_coverage``（0～1）并勾选要保留的状态
  （mapped / partial / ambiguous）；
- 每条路径按 ``seg2_covered / total`` 计算最终覆盖率；
- mapped / partial 行作为整体判断（通过则整行保留）；
- **歧义行按路径分别判断**：保留该行中全部通过阈值的路径候选，不自动
  替用户裁决；一个候选都不剩时整行丢弃；
- unmapped 行始终计入统计，但**绝不**写入筛选结果。

筛选产物（``KeptRow``）与 ``report.build_tsv2_rows`` 配合生成与原始
双段 TSV 同表头（25 列）的筛选 TSV。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .engine2 import RowResult2

# 可被筛选保留的状态；unmapped 永远不在此列（只统计、不落结果）
FILTERABLE_STATUSES = ("mapped", "partial", "ambiguous")


@dataclass(frozen=True)
class KeptRow:
    """通过筛选的一行（歧义行的 candidates 已按路径裁剪）。"""

    row: RowResult2
    candidates: tuple           # 通过阈值的 PathCandidate 元组


def _path_ratio(path) -> float:
    """单条路径的最终覆盖率：seg2_covered / total。"""
    if path.total <= 0:
        return 0.0
    return path.seg2_covered / path.total


def path_coverages(row: RowResult2) -> list[float]:
    """一行内各路径的最终覆盖率（按路径顺序，供预览展示）。"""
    return [_path_ratio(p) for p in row.candidates]


@dataclass(frozen=True)
class QualityFilterResult:
    kept: list[KeptRow]
    counts: dict[str, Any]


def apply_quality_filter(
    rows: list[RowResult2],
    min_coverage: float,
    statuses: tuple[str, ...],
) -> QualityFilterResult:
    """对双段结果执行质量筛选（纯函数，不修改传入的 rows）。

    ``min_coverage`` 必须在 [0, 1] 内（由接口层保证）；``statuses`` 为
    用户勾选要保留的状态（unmapped 即使出现也不会保留）。
    """
    wanted = set(statuses)
    kept: list[KeptRow] = []

    kept_counts = {"mapped": 0, "partial": 0, "ambiguous": 0, "unmapped": 0}
    dropped_counts = {"mapped": 0, "partial": 0, "ambiguous": 0, "unmapped": 0}
    dropped_by_threshold = 0        # 因覆盖率未达阈值而丢弃的行数
    dropped_by_status = 0           # 因状态未勾选而丢弃的行数（不含 unmapped）
    ambiguous_rows_total = 0
    ambiguous_paths_dropped = 0     # 歧义行中被阈值裁掉的路径数（行本身可能仍保留）

    for row in rows:
        status = row.status
        if status == "unmapped":
            # unmapped 始终统计，绝不写入筛选结果
            dropped_counts["unmapped"] += 1
            continue

        if status not in wanted:
            dropped_counts[status] += 1
            dropped_by_status += 1
            continue

        if status == "ambiguous":
            ambiguous_rows_total += 1
            passing = tuple(
                p for p in row.candidates if _path_ratio(p) >= min_coverage
            )
            ambiguous_paths_dropped += len(row.candidates) - len(passing)
            if not passing:
                dropped_counts["ambiguous"] += 1
                dropped_by_threshold += 1
                continue
            kept.append(KeptRow(row=row, candidates=passing))
            kept_counts["ambiguous"] += 1
            continue

        # mapped / partial：行内恰有一条路径，整体判断
        path = row.candidates[0]
        if _path_ratio(path) < min_coverage:
            dropped_counts[status] += 1
            dropped_by_threshold += 1
            continue
        kept.append(KeptRow(row=row, candidates=(path,)))
        kept_counts[status] += 1

    source_counts = {
        "mapped": kept_counts["mapped"] + dropped_counts["mapped"],
        "partial": kept_counts["partial"] + dropped_counts["partial"],
        "ambiguous": kept_counts["ambiguous"] + dropped_counts["ambiguous"],
        "unmapped": dropped_counts["unmapped"],
    }

    counts = {
        # 筛选后写入结果的行数（unmapped 永不计入）
        "total": sum(kept_counts[s] for s in FILTERABLE_STATUSES),
        "kept": kept_counts,
        # 原任务全量统计（含 unmapped），便于核对筛选前后数量
        "source_total": len(rows),
        "source": source_counts,
        # 丢弃明细
        "dropped_total": len(rows) - sum(kept_counts[s] for s in FILTERABLE_STATUSES),
        "dropped": dropped_counts,
        "dropped_by_threshold": dropped_by_threshold,
        "dropped_by_status": dropped_by_status,
        "dropped_unmapped": dropped_counts["unmapped"],
        # 歧义按路径分别判断的明细（裁掉的路径数不等于行数）
        "ambiguous_rows_total": ambiguous_rows_total,
        "ambiguous_paths_dropped": ambiguous_paths_dropped,
    }
    return QualityFilterResult(kept=kept, counts=counts)


def kept_row_to_dict(kr: KeptRow) -> dict[str, Any]:
    """筛选预览行：结构与双段预览一致，歧义行的 candidates 只含通过路径。

    路径的 path_index 等字段保持原序号，与原任务/裁决版 TSV 可直接对照。
    额外给出每个保留路径的最终覆盖率（``coverage``，0～1，保留 6 位小数）。
    """
    row = kr.row
    data = {
        "line_no": row.line_no,
        "src_chrom": row.src_chrom,
        "src_start": row.src_start,
        "src_end": row.src_end,
        "status": row.status,
        "failed_stage": row.failed_stage,
        "reason": row.reason,
        "candidates": [],
    }
    for path in kr.candidates:
        ratio = _path_ratio(path)
        cand = {
            "path_index": path.path_index,
            "chain1_id": path.chain1_id,
            "chain2_id": path.chain2_id,
            "mid_chrom": path.mid_chrom,
            "mid_strand": path.mid_strand,
            "dest_chrom": path.dest_chrom,
            "dest_strand": path.dest_strand,
            "seg1_covered": path.seg1_covered,
            "seg2_covered": path.seg2_covered,
            "total": path.total,
            "coverage": round(ratio, 6),
            "uncovered_src": [list(iv) for iv in path.uncovered_src],
            "fragments": [
                {
                    "src_start": f.src_start,
                    "src_end": f.src_end,
                    "mid_start": f.mid_start,
                    "mid_end": f.mid_end,
                    "dest_start": f.dest_start,
                    "dest_end": f.dest_end,
                }
                for f in path.fragments
            ],
        }
        data["candidates"].append(cand)
    return data


def trimmed_row(kr: KeptRow) -> RowResult2:
    """返回只含通过路径的 RowResult2 副本（供 TSV 构建复用原列逻辑）。"""
    return replace(kr.row, candidates=list(kr.candidates))
