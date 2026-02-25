"""Relationship engine — pure Python graph traversal.

Takes people + relationships, computes named relationships from a player's
perspective (e.g. "maternal grandmother", "paternal uncle").

No DB, no I/O — pure functions on in-memory data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Person:
    id: str
    name: str
    nickname: str | None = None
    maiden_name: str | None = None
    born: int | None = None
    status: str = "living"
    player: bool = False
    placeholder: bool = False


@dataclass
class Relationship:
    id: str
    type: str  # married, parent_of, divorced
    from_id: str
    to_id: str


@dataclass
class NamedRelation:
    """A resolved relationship label from the player's perspective."""
    person: Person
    label: str  # e.g. "maternal grandmother", "sibling", "paternal uncle"
    generation: int  # 0=same, 1=parent, 2=grandparent, -1=child
    difficulty: int  # 1=easy, 2=medium, 3=hard, 4=chain


class FamilyGraph:
    """In-memory family graph for computing relationships."""

    def __init__(self, people: list[Person], relationships: list[Relationship]):
        self._people = {p.id: p for p in people}
        self._rels = relationships

        # Build adjacency: parent_of edges
        self._parents: dict[str, list[str]] = {}  # child_id -> [parent_ids]
        self._children: dict[str, list[str]] = {}  # parent_id -> [child_ids]
        self._spouses: dict[str, list[str]] = {}  # person_id -> [spouse_ids]

        for r in relationships:
            if r.type == "parent_of":
                self._children.setdefault(r.from_id, []).append(r.to_id)
                self._parents.setdefault(r.to_id, []).append(r.from_id)
            elif r.type in ("married", "divorced"):
                self._spouses.setdefault(r.from_id, []).append(r.to_id)
                self._spouses.setdefault(r.to_id, []).append(r.from_id)

    def _get(self, pid: str) -> Person | None:
        return self._people.get(pid)

    def _parents_of(self, pid: str) -> list[str]:
        return self._parents.get(pid, [])

    def _children_of(self, pid: str) -> list[str]:
        return self._children.get(pid, [])

    def _spouses_of(self, pid: str) -> list[str]:
        return self._spouses.get(pid, [])

    def _side_label(self, parent_id: str, player_id: str) -> str:
        """Determine 'maternal' or 'paternal' based on which parent path."""
        parents = self._parents_of(player_id)
        if len(parents) < 2:
            return ""
        # Heuristic: first parent listed is typically one side
        # We trace which parent branch this ancestor comes from
        for i, pid in enumerate(parents):
            if self._is_ancestor_of(parent_id, pid):
                return "maternal" if i == 1 else "paternal"
            if parent_id == pid:
                return "maternal" if i == 1 else "paternal"
        return ""

    def _is_ancestor_of(self, ancestor_id: str, person_id: str, depth: int = 0) -> bool:
        if depth > 10:
            return False
        if ancestor_id == person_id:
            return True
        for parent in self._parents_of(person_id):
            if self._is_ancestor_of(ancestor_id, parent, depth + 1):
                return True
        return False

    def compute_relations(self, player_id: str) -> list[NamedRelation]:
        """Compute all named relationships from player's perspective."""
        results: list[NamedRelation] = []
        player = self._get(player_id)
        if not player:
            return results

        seen: set[str] = {player_id}

        # --- Parents (generation +1, difficulty 1) ---
        parents = self._parents_of(player_id)
        for pid in parents:
            p = self._get(pid)
            if not p:
                continue
            seen.add(pid)
            results.append(NamedRelation(person=p, label="parent", generation=1, difficulty=1))

        # --- Siblings (generation 0, difficulty 1) ---
        siblings: set[str] = set()
        for pid in parents:
            for child_id in self._children_of(pid):
                if child_id != player_id:
                    siblings.add(child_id)
        for sid in siblings:
            s = self._get(sid)
            if not s:
                continue
            seen.add(sid)
            results.append(NamedRelation(person=s, label="sibling", generation=0, difficulty=1))

        # --- Grandparents (generation +2, difficulty 2) ---
        grandparents: list[tuple[str, str]] = []  # (gp_id, side)
        for pid in parents:
            gps = self._parents_of(pid)
            side = self._side_label(pid, player_id)
            for gp_id in gps:
                grandparents.append((gp_id, side))

        for gp_id, side in grandparents:
            gp = self._get(gp_id)
            if not gp or gp_id in seen:
                continue
            seen.add(gp_id)
            label = f"{side} grandparent".strip() if side else "grandparent"
            results.append(NamedRelation(person=gp, label=label, generation=2, difficulty=2))

        # --- Great-grandparents (generation +3, difficulty 3) ---
        for gp_id, side in grandparents:
            for ggp_id in self._parents_of(gp_id):
                ggp = self._get(ggp_id)
                if not ggp or ggp_id in seen:
                    continue
                seen.add(ggp_id)
                label = f"{side} great-grandparent".strip() if side else "great-grandparent"
                results.append(NamedRelation(person=ggp, label=label, generation=3, difficulty=3))

        # --- Aunts/Uncles (generation +1, difficulty 2) ---
        aunts_uncles: set[str] = set()
        for pid in parents:
            parent_parents = self._parents_of(pid)
            for gp_id in parent_parents:
                for au_id in self._children_of(gp_id):
                    if au_id not in parents and au_id != player_id:
                        aunts_uncles.add(au_id)

        for au_id in aunts_uncles:
            au = self._get(au_id)
            if not au or au_id in seen:
                continue
            seen.add(au_id)
            results.append(NamedRelation(person=au, label="aunt/uncle", generation=1, difficulty=2))

            # Their spouses are also aunts/uncles (by marriage)
            for sp_id in self._spouses_of(au_id):
                sp = self._get(sp_id)
                if sp and sp_id not in seen:
                    seen.add(sp_id)
                    results.append(NamedRelation(person=sp, label="aunt/uncle (by marriage)", generation=1, difficulty=2))

        # --- Great-aunts/uncles (generation +2, difficulty 3) ---
        for gp_id, side in grandparents:
            gp_parents = self._parents_of(gp_id)
            for ggp_id in gp_parents:
                for gau_id in self._children_of(ggp_id):
                    if gau_id != gp_id and gau_id not in seen:
                        gau = self._get(gau_id)
                        if gau:
                            seen.add(gau_id)
                            label = f"{side} great-aunt/uncle".strip() if side else "great-aunt/uncle"
                            results.append(NamedRelation(person=gau, label=label, generation=2, difficulty=3))

        # --- Cousins (generation 0, difficulty 3) ---
        for au_id in aunts_uncles:
            for cousin_id in self._children_of(au_id):
                cousin = self._get(cousin_id)
                if cousin and cousin_id not in seen:
                    seen.add(cousin_id)
                    results.append(NamedRelation(person=cousin, label="cousin", generation=0, difficulty=3))

        # --- Spouses of player (generation 0, difficulty 1) — unlikely for children but complete ---
        for sp_id in self._spouses_of(player_id):
            sp = self._get(sp_id)
            if sp and sp_id not in seen:
                seen.add(sp_id)
                results.append(NamedRelation(person=sp, label="spouse", generation=0, difficulty=1))

        # --- In-laws: spouses of parents' siblings ---
        # Already handled above via aunt/uncle by marriage

        return results
