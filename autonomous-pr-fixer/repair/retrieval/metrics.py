"""File-level localization metrics.

For one instance, `ranking` is a list of repository-relative file paths, best first, and `gold`
is the set of non-test files the reference fix changed.

- recall@k: fraction of gold files that appear in the top k (averaged over instances).
- all@k: 1 if every gold file appears in the top k, else 0 (strict; reported alongside recall).
- MRR: 1 / rank of the first gold file in the ranking, 0 if none is ranked.
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence

from pydantic import BaseModel

KS = (1, 5, 10)


class InstanceScore(BaseModel):
    instance_id: str
    recall: dict[int, float]
    all_found: dict[int, bool]
    reciprocal_rank: float
    first_gold_rank: int | None
    ranked: int


def score_instance(instance_id: str, ranking: Sequence[str], gold: set[str], ks: Sequence[int] = KS) -> InstanceScore:
    if not gold:
        raise ValueError(f"{instance_id}: empty gold file set")
    # De-duplicate while keeping order: a method that lists a file twice gets no extra credit.
    seen: list[str] = list(dict.fromkeys(ranking))
    first = next((i + 1 for i, path in enumerate(seen) if path in gold), None)
    recall = {k: len(gold & set(seen[:k])) / len(gold) for k in ks}
    all_found = {k: gold <= set(seen[:k]) for k in ks}
    return InstanceScore(
        instance_id=instance_id,
        recall=recall,
        all_found=all_found,
        reciprocal_rank=1.0 / first if first else 0.0,
        first_gold_rank=first,
        ranked=len(seen),
    )


class AggregateScore(BaseModel):
    n: int
    recall: dict[int, float]
    all_found: dict[int, float]
    mrr: float
    not_ranked: int  # instances where no gold file appeared anywhere in the ranking


def aggregate(scores: Sequence[InstanceScore], ks: Sequence[int] = KS) -> AggregateScore:
    if not scores:
        raise ValueError("no scores to aggregate")
    return AggregateScore(
        n=len(scores),
        recall={k: round(statistics.mean(s.recall[k] for s in scores), 4) for k in ks},
        all_found={k: round(statistics.mean(1.0 if s.all_found[k] else 0.0 for s in scores), 4) for k in ks},
        mrr=round(statistics.mean(s.reciprocal_rank for s in scores), 4),
        not_ranked=sum(1 for s in scores if s.first_gold_rank is None),
    )
