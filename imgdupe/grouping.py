"""Cluster images into groups of possible duplicates.

Pairwise comparison of every image against every other is quadratic and falls
over on a real photo library. Instead each 64-bit hash is split into 16 bands
of 4 bits and indexed; by the pigeonhole principle two hashes within a Hamming
distance of 15 must agree on at least one band, so only images sharing a band
are ever compared in full.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .hashing import bits_distance
from .quality import Candidate, explain_choice, rank

_BANDS = 16
_BAND_BITS = 64 // _BANDS
_BAND_MASK = (1 << _BAND_BITS) - 1


@dataclass(slots=True)
class DuplicateGroup:
    members: list[Candidate]
    exact: bool = False
    keeper_reasons: list[str] = field(default_factory=list)

    @property
    def keeper(self) -> Candidate:
        return self.members[0]

    @property
    def removable(self) -> list[Candidate]:
        return self.members[1:]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.size for c in self.removable)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _band_index(values: list[int]) -> dict[tuple[int, int], list[int]]:
    index: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, value in enumerate(values):
        for band in range(_BANDS):
            index[(band, (value >> (band * _BAND_BITS)) & _BAND_MASK)].append(i)
    return index


def _candidate_pairs(values: list[int]) -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for bucket in _band_index(values).values():
        if len(bucket) < 2:
            continue
        # A band shared by a huge number of images (flat scans, blank pages)
        # carries no signal and would explode into millions of comparisons.
        if len(bucket) > 2000:
            continue
        for a_pos, a in enumerate(bucket):
            for b in bucket[a_pos + 1:]:
                pairs.add((a, b) if a < b else (b, a))
    return pairs


def build_groups(candidates: list[Candidate], threshold: int) -> list[DuplicateGroup]:
    """Group candidates whose perceptual hashes fall within ``threshold`` bits."""
    if len(candidates) < 2:
        return []

    uf = _UnionFind(len(candidates))
    exact_members: set[int] = set()

    by_content: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i, c in enumerate(candidates):
        by_content[(c.size, c.sig.content_md5)].append(i)
    for bucket in by_content.values():
        for other in bucket[1:]:
            uf.union(bucket[0], other)
            exact_members.update((bucket[0], other))

    phashes = [c.sig.phash for c in candidates]
    dhashes = [c.sig.dhash for c in candidates]

    for a, b in _candidate_pairs(phashes) | _candidate_pairs(dhashes):
        p_dist = bits_distance(phashes[a], phashes[b])
        d_dist = bits_distance(dhashes[a], dhashes[b])
        # Either signature matching tightly is enough, but a near miss on one
        # requires corroboration from the other.
        if min(p_dist, d_dist) <= threshold and (p_dist + d_dist) <= threshold * 3:
            uf.union(a, b)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(candidates)):
        clusters[uf.find(i)].append(i)

    groups: list[DuplicateGroup] = []
    for indices in clusters.values():
        if len(indices) < 2:
            continue
        members = rank([candidates[i] for i in indices])
        group = DuplicateGroup(
            members=members,
            exact=all(i in exact_members for i in indices),
        )
        group.keeper_reasons = explain_choice(group.keeper, group.removable)
        groups.append(group)

    groups.sort(key=lambda g: -g.reclaimable_bytes)
    return groups
