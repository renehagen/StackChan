"""Match spoken names against Home Assistant's Assist catalogue."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from unicodedata import normalize

import yaml


_YAML_LOADER = getattr(yaml, "CBaseLoader", yaml.BaseLoader)
_DIRECTIONS = frozenset({"left", "right", "links", "rechts", "boven", "beneden", "voor", "achter"})
_TYPE_WORDS = {"lamp": "light", "lampen": "light", "lampgroep": "light", "script": "script",
               "scene": "scene", "scène": "scene"}


def name_key(name: str) -> str:
    return "".join(char for char in normalize("NFKD", name.casefold()) if char.isalnum())


def _items(value: object) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(", ") if part.strip()) if isinstance(value, str) else ()


@dataclass(frozen=True)
class Target:
    names: tuple[str, ...]
    domain: str
    areas: tuple[str, ...] = ()
    tool: str | None = None

    def reference(self) -> dict:
        item = {"name": self.names[0], "domain": self.domain, "area": self.areas[0] if self.areas else None}
        if self.tool:
            item["tool"] = self.tool
        return item


def context_targets(text: str) -> list[Target]:
    """Decode the native GetLiveContext overview, including its aliases."""
    if text.startswith("Live Context:"):
        text = text.partition("\n")[2]
    if not text.lstrip().startswith("- names:"):
        return []
    try:
        rows = yaml.load(text, Loader=_YAML_LOADER)
    except yaml.YAMLError:
        return []
    targets = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        names = _items(row.get("names"))
        domain = row.get("domain")
        if names and isinstance(domain, str) and re.fullmatch(r"[a-z_]+", domain):
            targets.append(Target(names, domain, _items(row.get("areas"))))
    return targets


def _name_variants(query: str) -> list[tuple[str, str | None]]:
    variants = [(query, None)]
    folded = query.casefold()
    for word, domain in _TYPE_WORDS.items():
        if folded.startswith(word) and folded[len(word):len(word) + 1] in {" ", "-", "_"}:
            variants.append((query[len(word):].lstrip(" -_"), domain))
        if folded.endswith(word) and len(query) >= len(word) + 3:
            variants.append((query[:-len(word)].rstrip(" -_"), domain))
    return variants


def _score(query: str, name: str, *, stripped: bool) -> float:
    if query.casefold() == name.casefold():
        return 1.0 if stripped else 1.2
    left, right = name_key(query), name_key(name)
    if (not left or not right
            or re.findall(r"\d+", normalize("NFKD", query)) != re.findall(r"\d+", normalize("NFKD", name))):
        return 0.0
    if left == right:
        return 1.0 if stripped else 1.1
    # Keep model numbers and explicit locations/directions distinct.
    if (min(len(left), len(right)) < 6
            or _DIRECTIONS.intersection(re.findall(r"\w+", query.casefold()))
            != _DIRECTIONS.intersection(re.findall(r"\w+", name.casefold()))):
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    edits = sum(max(b - a, d - c) for op, a, b, c, d in matcher.get_opcodes() if op != "equal")
    score = matcher.ratio()
    return score if edits <= (2 if max(len(left), len(right)) >= 10 else 1) and score >= .84 else 0.0


def find_targets(query: str, targets: list[Target], *, domains: tuple[str, ...] = (),
                 area: str | None = None) -> list[Target]:
    variants = _name_variants(query)
    area_key = name_key(area) if area else None
    scored = []
    for target in targets:
        if domains and target.domain not in domains:
            continue
        if area_key and area_key not in {name_key(value) for value in target.areas}:
            continue
        score = max((_score(value, name, stripped=bool(inferred))
                     for value, inferred in variants if inferred in (None, target.domain)
                     for name in target.names), default=0.0)
        if score:
            scored.append((score, target))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    # Exact and normalized matches outrank edits. Close edit scores are ambiguous.
    matches = [target for score, target in scored if score == best or best < 1 and best - score < .09]
    if len(matches) == 1 and not matches[0].tool:
        chosen = matches[0]
        # The downstream Assist command uses this exact name, domain and area.
        # Its canonical name may also belong to another row in the same scope.
        matches.extend(target for target in targets if target is not chosen and target.domain == chosen.domain
                       and (not chosen.areas or name_key(chosen.areas[0]) in {name_key(a) for a in target.areas})
                       and chosen.names[0].casefold() in {name.casefold() for name in target.names})
    return matches
