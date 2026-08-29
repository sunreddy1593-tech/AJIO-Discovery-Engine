"""Co-occurrence: lift matrices, Jaccard clusters, and the short list the report quotes.

Lift is P(A∩B) / (P(A)P(B)) over the tagged analyzable set (architecture.md
§8.2). The three matrices the plan names are blocker × uncertainty, blocker ×
segment, and blocker × info-sought.

High-Jaccard tag pairs are merged into one opportunity cluster (edge-case.md
§5.11) so the same supporting documents are not ranked twice.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Label = tuple[str, str]  # (dimension, value)

#: When naming a merged cluster, prefer the blocker as the opportunity's face.
_DIMENSION_PRIORITY = {
    "blocker_type": 0,
    "uncertainty_type": 1,
    "info_sought_elsewhere": 2,
    "wishlist_motivation": 3,
    "segment_cue": 4,
}

#: "Top 2–3" in the stage brief: three is the ceiling.
TOP_N = 3

LIFT_PAIRS: tuple[tuple[str, str], ...] = (
    ("blocker_type", "uncertainty_type"),
    ("blocker_type", "segment_cue"),
    ("blocker_type", "info_sought_elsewhere"),
)


def top_cooccurring(
    target: Label,
    membership: Mapping[Label, Sequence[str]],
    *,
    k: int = TOP_N,
    exclude: Sequence[Label] = (),
    doc_ids: Sequence[str] | None = None,
) -> str:
    """Return the ``k`` labels that most often share a document with ``target``.

    ``doc_ids`` overrides the target's membership (used for a merged cluster's
    union). ``exclude`` drops tags already inside the cluster so a merge partner
    is not also listed as a co-occurrence.
    """
    skip = set(exclude) | {target}
    target_docs = set(doc_ids) if doc_ids is not None else set(membership.get(target, ()))
    if not target_docs or k <= 0:
        return ""
    ranked: list[tuple[int, str, str]] = []
    for (dimension, label), docs in membership.items():
        if (dimension, label) in skip:
            continue
        overlap = len(target_docs.intersection(docs))
        if overlap == 0:
            continue
        ranked.append((overlap, dimension, label))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    return "; ".join(f"{dimension}={label}" for _, dimension, label in ranked[:k])


@dataclass(frozen=True)
class TagCluster:
    """One candidate opportunity: a tag, or several tags that share most of their docs."""

    primary: Label
    members: tuple[Label, ...]
    doc_ids: frozenset[str]

    @property
    def dimension(self) -> str:
        return self.primary[0]

    @property
    def label(self) -> str:
        return self.primary[1]

    def member_csv(self) -> str:
        return "; ".join(f"{dimension}={label}" for dimension, label in self.members)


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    """|A∩B| / |A∪B|. Zero when both sets are empty."""
    left, right = set(a), set(b)
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def cluster_tags(
    membership: Mapping[Label, Sequence[str]],
    *,
    dimensions: Sequence[str] | None = None,
    jaccard_min: float = 0.5,
) -> list[TagCluster]:
    """Union-find merge of tags whose supporting-document Jaccard is ≥ ``jaccard_min``.

    ``jaccard_min <= 0`` disables merging (each observed tag is its own cluster).
    Tags outside ``dimensions`` are ignored, so ``segment_cue`` can stay a
    breakdown and never become an opportunity area.
    """
    keys = [
        key
        for key in membership
        if dimensions is None or key[0] in dimensions
    ]
    if not keys:
        return []
    if jaccard_min <= 0:
        return [
            TagCluster(
                primary=key,
                members=(key,),
                doc_ids=frozenset(membership[key]),
            )
            for key in sorted(keys)
        ]

    parent = {key: key for key in keys}

    def find(key: Label) -> Label:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: Label, right: Label) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_r] = root_l

    pairs: list[tuple[float, Label, Label]] = []
    for index, left in enumerate(keys):
        docs_left = set(membership[left])
        for right in keys[index + 1 :]:
            score = jaccard(docs_left, membership[right])
            if score >= jaccard_min:
                pairs.append((score, left, right))
    pairs.sort(key=lambda row: (-row[0], row[1], row[2]))
    for _score, left, right in pairs:
        union(left, right)

    groups: dict[Label, list[Label]] = defaultdict(list)
    for key in keys:
        groups[find(key)].append(key)

    clusters: list[TagCluster] = []
    for members in groups.values():
        members = tuple(
            sorted(
                members,
                key=lambda item: (
                    _DIMENSION_PRIORITY.get(item[0], 9),
                    item[0],
                    item[1],
                ),
            )
        )
        primary = min(
            members,
            key=lambda item: (
                _DIMENSION_PRIORITY.get(item[0], 9),
                -len(membership[item]),
                item[1],
            ),
        )
        doc_ids: set[str] = set()
        for member in members:
            doc_ids.update(membership[member])
        clusters.append(TagCluster(primary=primary, members=members, doc_ids=frozenset(doc_ids)))
    clusters.sort(key=lambda cluster: (cluster.dimension, cluster.label))
    return clusters


def lift(
    n_both: int,
    n_a: int,
    n_b: int,
    n_total: int,
) -> float | None:
    """P(A∩B) / (P(A)P(B)). None when either marginal is zero."""
    if n_total <= 0 or n_a <= 0 or n_b <= 0:
        return None
    p_both = n_both / n_total
    p_a = n_a / n_total
    p_b = n_b / n_total
    denom = p_a * p_b
    if denom == 0:
        return None
    return p_both / denom


def lift_rows(
    membership: Mapping[Label, Sequence[str]],
    n_total: int,
    *,
    pairs: Sequence[tuple[str, str]] = LIFT_PAIRS,
) -> list[dict]:
    """One row per observed pair in the three named matrices. No invented zeros."""
    by_dim: dict[str, list[Label]] = {}
    for key in membership:
        by_dim.setdefault(key[0], []).append(key)
    rows: list[dict] = []
    for dim_a, dim_b in pairs:
        for a in sorted(by_dim.get(dim_a, [])):
            docs_a = set(membership[a])
            for b in sorted(by_dim.get(dim_b, [])):
                docs_b = set(membership[b])
                n_both = len(docs_a & docs_b)
                if n_both == 0:
                    continue
                value = lift(n_both, len(docs_a), len(docs_b), n_total)
                if value is None:
                    continue
                rows.append(
                    {
                        "dimension_a": a[0],
                        "label_a": a[1],
                        "dimension_b": b[0],
                        "label_b": b[1],
                        "n_both": n_both,
                        "n_a": len(docs_a),
                        "n_b": len(docs_b),
                        "lift": value,
                    }
                )
    rows.sort(key=lambda r: (-r["lift"], r["label_a"], r["label_b"]))
    return rows


def segment_matrix_rows(
    membership: Mapping[Label, Sequence[str]],
    n_total: int,
) -> list[dict]:
    """``segment_cue`` × ``blocker_type``: n, rate in-segment, and lift."""
    return [
        {
            "segment": row["label_a"],
            "blocker_type": row["label_b"],
            "n_docs": row["n_both"],
            "lift": row["lift"],
        }
        for row in lift_rows(
            membership,
            n_total,
            pairs=(("segment_cue", "blocker_type"),),
        )
    ]
