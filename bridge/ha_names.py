"""Match spoken names against Home Assistant's Assist catalogue."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from unicodedata import normalize

import yaml


_YAML_LOADER = getattr(yaml, "CBaseLoader", yaml.BaseLoader)
_DIRECTIONS = frozenset({"left", "right", "links", "rechts", "boven", "beneden", "voor", "achter"})
_SCENE_DIRECTIONS = _DIRECTIONS | {"open", "dicht", "gesloten", "omhoog", "omlaag", "aan", "uit", "on", "off"}
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
    state: str | None = None

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
            state = row.get("state")
            targets.append(Target(names, domain, _items(row.get("areas")), state=state if isinstance(state, str) else None))
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


def _lightstrip_name(label: str) -> str | None:
    # Only strip the separate Dutch preposition, preserving names such as Vanessa.
    label = re.sub(r"^((?:led|light|licht)[ _-]*strip)[ _-]+van[ _-]+", r"\1 ", label, flags=re.I)
    key = name_key(label)
    for noun in ("ledstrip", "lightstrip", "lichtstrip"):
        if key.startswith(noun) and len(key) - len(noun) >= 3:
            return key[len(noun):]
        if key.endswith(noun) and len(key) - len(noun) >= 3:
            return key[:-len(noun)]
    return None


def _words(label: str) -> tuple[str, ...]:
    return tuple(map(name_key, re.findall(r"[^\W_]+", normalize("NFC", label))))


def _scene_room_in_name(target: Target, area: str) -> bool:
    room = _words(area)
    if not room:
        return False
    for name in target.names:
        words = _words(name)
        if any(words[i:i + len(room)] == room for i in range(len(words) - len(room) + 1)):
            return True
    return False


def _score(query: str, name: str, *, stripped: bool, domain: str) -> float:
    if query.casefold() == name.casefold():
        return 1.0 if stripped else 1.2
    left, right = name_key(query), name_key(name)
    if (not left or not right
            or re.findall(r"\d+", normalize("NFKD", query)) != re.findall(r"\d+", normalize("NFKD", name))):
        return 0.0
    if left == right:
        return 1.0 if stripped else 1.1
    strip_names = False
    if domain == "light":
        query_strip, target_strip = _lightstrip_name(query), _lightstrip_name(name)
        if query_strip and target_strip:
            left, right = query_strip, target_strip
            if left == right:
                return 1.0
            strip_names = True
    # Keep model numbers and explicit locations/directions distinct.
    directions = _SCENE_DIRECTIONS if domain == "scene" else _DIRECTIONS
    if (min(len(left), len(right)) < (5 if strip_names else 6)
            or directions.intersection(_words(query)) != directions.intersection(_words(name))):
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    edits = sum(max(b - a, d - c) for op, a, b, c, d in matcher.get_opcodes() if op != "equal")
    score = matcher.ratio()
    minimum = .80 if strip_names and min(len(left), len(right)) == 5 else .84
    return score if edits <= (2 if max(len(left), len(right)) >= 10 else 1) and score >= minimum else 0.0


def scoped_targets(targets: list[Target], domains: tuple[str, ...], area: str | None) -> list[Target]:
    area_key = name_key(area) if area else None
    scene_only = domains == ("scene",)
    scoped = []
    for target in targets:
        if domains and target.domain not in domains:
            continue
        if area_key and area_key not in {name_key(value) for value in target.areas}:
            if not (scene_only and not target.areas and _scene_room_in_name(target, area)):
                continue
        scoped.append(target)
    return scoped


def find_targets(query: str, targets: list[Target], *, domains: tuple[str, ...] = (),
                 area: str | None = None) -> list[Target]:
    variants = _name_variants(query)
    scene_only = domains == ("scene",)
    leads = {_words(value) for value, inferred in variants if inferred in (None, "scene")} if scene_only else set()
    if leads and area:
        room = _words(area)
        leads |= {lead + room for lead in leads}
    leads = {lead for lead in leads if len(lead) >= 2}
    scored = []
    prefixes: list[tuple[Target, bool]] = []
    for target in scoped_targets(targets, domains, area):
        score = max((_score(value, name, stripped=bool(inferred), domain=target.domain)
                     for value, inferred in variants if inferred in (None, target.domain)
                     for name in target.names), default=0.0)
        if score:
            scored.append((score, target))
        elif leads:
            # A shortened title must match whole words. Keep competing titles
            # in the results so an omitted qualifier cannot pick one silently.
            tails = [words[len(lead):] for words in map(_words, target.names)
                     for lead in leads if len(lead) < len(words) and words[:len(lead)] == lead]
            if tails:
                safe = any(not _SCENE_DIRECTIONS.intersection(tail)
                           and not any(re.search(r"\d", word) for word in tail) for tail in tails)
                prefixes.append((target, safe))
    if not scored:
        matches = [target for target, safe in prefixes if safe or len(prefixes) > 1]
    else:
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
