"""Interpretable bilingual metadata routing with conservative abstention.

Selection suggests skills; it does not run them or grant any permissions.
Only names, descriptions, tags, triggers, approval, and bounded feedback are read.
Skill bodies and arbitrary extra candidate properties are never inspected.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from functools import lru_cache
import math
import re

from .language import DOMAIN_PARENTS, GENERIC_WORDS, STOP_WORDS, features, normalize, runtime_info

MAX_QUERY_CHARS = 6000
MAX_CANDIDATES = 20000
MAX_RESULTS = 10
_EXPLICIT = re.compile(r"(?<![\w$])\$([\w][\w:.-]{0,159})(?![\w:/\\-])", re.UNICODE)
_NEGATION = re.compile(r"\b(?:not|never|neither|nor|without|avoid|don't|dont|cannot|can't|skip|ekki|aldrei|hvorki|né|án|sleppa|slepptu)\b")
_CLAUSE = re.compile(r"[;!?\n]+|\.(?=\s|$)|,\s*|\b(?:but|instead|en|heldur)\b", re.IGNORECASE)
_CONJUNCTION = re.compile(r"\b(?:and|then|also|og|síðan|líka)\b", re.IGNORECASE)
_SOFT_DOMAINS = frozenset({"document", "research", "finance", "design", "test", "security"})
_DOMAIN_ACTION_FALLBACK = frozenset({"read", "analyze", "review", "search", "summarize", "compare"})
_CHANGE_ACTIONS = frozenset({"create", "edit", "convert", "save", "install", "schedule", "publish", "fix"})
_ACTION_COMPATIBILITY = {"fix": {"edit"}, "convert": {"create", "edit"},
                         "summarize": {"read", "analyze", "convert"}, "extract": {"read", "convert"},
                         "read": {"review", "extract"}, "test": {"review", "analyze"}, "create": {"visualize"}}


def _compatible_actions(actions: frozenset[str]) -> set[str]:
    matches = set(actions)
    for action in actions:
        matches.update(_ACTION_COMPATIBILITY.get(action, ()))
    return matches


def _small(item: Mapping, *, reason: str | None = None) -> dict:
    result = {"id": item["id"], "name": item["name"]}
    if isinstance(item.get("source"), str) and len(item["source"]) <= 300:
        result["source"] = item["source"]
    if reason is not None:
        result["reason"] = reason
    return result


def _result(decision: str, *, reason: str, selected=(), candidates=(), **diagnostics) -> dict:
    return {
        "decision": decision,
        "selected": list(selected),
        "candidates": list(candidates),
        "explanations": [reason],
        "diagnostics": {"language": runtime_info(), **diagnostics},
    }


def _metadata(candidate: Mapping) -> dict | None:
    if not isinstance(candidate, Mapping) or candidate.get("approved") is False:
        return None
    cid, name, description = candidate.get("id"), candidate.get("name"), candidate.get("description", "")
    if not all(isinstance(value, str) for value in (cid, name, description)):
        return None
    if not cid or not name or len(cid) > 240 or len(name) > 160 or len(description) > 8000:
        return None
    fields = {}
    for key in ("tags", "triggers"):
        value = candidate.get(key, ())
        if not isinstance(value, (tuple, list)) or len(value) > 64:
            return None
        if any(not isinstance(v, str) or len(v) > 160 for v in value):
            return None
        fields[key] = tuple(value)
    return {"id": cid, "name": name, "description": description, **fields,
            "source": candidate.get("source"), "feedback": candidate.get("feedback")}


def _parts(text: str) -> tuple[list[str], list[str]]:
    """Separate positive and negated clauses; no truncated negation context."""
    positive, negative = [], []
    normalized = normalize(text)
    # Skill identifiers can themselves contain conjunctions or periods. Mask
    # exact mentions before clause parsing, then restore their complete names.
    mentions = []
    def mask(match):
        mentions.append(match.group(0))
        return f" xskillmention{len(mentions) - 1}x "
    normalized = _EXPLICIT.sub(mask, normalized)
    normalized = re.sub(r"\b(?:rather than|instead of|í stað(?:inn)? fyrir)\b", " without ", normalized)
    scope_negative = False
    for clause in _CLAUSE.split(normalized):
        if not clause.strip():
            continue
        # A new affirmative operation after a conjunction starts a new scope:
        # "do not edit PDFs and create a spreadsheet". Plain coordinated objects
        # remain under the same negation: "do not use PDF and Word".
        chunks = _CONJUNCTION.split(clause)
        for index, chunk in enumerate(chunks):
            for number, mention in enumerate(mentions):
                chunk = chunk.replace(f"xskillmention{number}x", mention)
            negation = _NEGATION.search(chunk)
            if negation:
                before, after = chunk[:negation.start()], chunk[negation.end():]
                # "create a PDF, not a spreadsheet" keeps its affirmative prefix.
                if before.strip() and features(before)["actions"]:
                    positive.append(before)
                if after.strip():
                    negative.append(after)
                scope_negative = True
            elif scope_negative and not features(chunk)["actions"] and not re.search(r"\b(?:use|nota|notaðu|nýta|nýttu)\b", chunk):
                negative.append(chunk)
            else:
                positive.append(chunk)
                scope_negative = False
    return positive, negative


@lru_cache(maxsize=4096)
def _profile(name: str, description: str, tags: tuple, triggers: tuple) -> dict:
    positive, _ = _parts(description)
    identity = features(" ".join((name, *tags)))
    detail = features(" ".join((*positive, *triggers)))
    primary = set(identity["domains"] or detail["domains"])
    for specific, parents in DOMAIN_PARENTS.items():
        if specific in detail["domains"] and primary & parents:
            primary.add(specific)
    for specific, parents in DOMAIN_PARENTS.items():
        if specific in primary:
            primary -= parents - {"trading"}
    if primary - _SOFT_DOMAINS:
        primary -= {"research", "design", "test", "finance"}
    # Generic "skill", "assistant" and task verbs are not useful identity anchors.
    anchors = identity["terms"] - GENERIC_WORDS - STOP_WORDS
    return {"primary": frozenset(primary), "domains": identity["domains"] | detail["domains"],
            "actions": detail["actions"] | identity["actions"],
            "terms": detail["terms"] | identity["terms"], "anchors": anchors,
            "triggers": tuple(normalize(t).strip() for t in triggers if len(t.strip()) >= 4)}


def _feedback(item: Mapping) -> float:
    """At most a tiny rank adjustment, never relevance or ambiguity evidence."""
    feedback = item.get("feedback")
    if not isinstance(feedback, Mapping):
        return 0.0
    success, failure = feedback.get("success", 0), feedback.get("failure", 0)
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
           or n < 0 or n > 1e12 for n in (success, failure)):
        return 0.0
    return 0.08 * (success - failure) / (success + failure + 4)


def route(query: str, candidates: Sequence[Mapping], *, limit: int = 3) -> dict:
    """Rank approved metadata, choose clear matches, or explain abstention.

    ``approved=False`` excludes a candidate. Missing approval is accepted for
    callers supplying an already vetted catalog. Unknown explicit names never
    fall back to a similarly named skill. The supplied pool must be complete and
    at most 20,000 rows; an oversized query/pool fails closed, without truncation.
    ``limit`` bounds returned candidates; an incomplete multi-intent selection is
    reported as ambiguous with no selected skills.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("limit must be an integer between 1 and 10")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise TypeError("candidates must be a sequence of metadata mappings")
    if len(query) > MAX_QUERY_CHARS:
        return _result("bypass", reason="query_too_long: no text was silently truncated")
    if len(candidates) > MAX_CANDIDATES:
        return _result("bypass", reason="catalog_too_large: provide a complete bounded candidate pool")
    if not query.strip():
        return _result("bypass", reason="empty_query")

    items, ids = [], set()
    for candidate in candidates:
        item = _metadata(candidate)
        if item is not None:
            if item["id"] in ids:
                return _result("ambiguous", reason="duplicate_candidate_id: catalog identity must be unique")
            items.append(item)
            ids.add(item["id"])
    positive, negative = _parts(query)
    explicit = [match.group(1).rstrip(".") for part in positive for match in _EXPLICIT.finditer(part)]
    denied_explicit = {match.group(1).rstrip(".") for part in negative for match in _EXPLICIT.finditer(part)}
    if explicit:
        matches = []
        for target in dict.fromkeys(explicit):
            if target in denied_explicit:
                return _result("ambiguous", reason="conflicting_explicit_request")
            exact = [item for item in items if target in {normalize(item["id"]), normalize(item["name"])}]
            if not exact:
                # A namespaced skill can be explicitly named by its complete short
                # component only if it is unique. Never prefix/fuzzy-match names.
                exact = [item for item in items if normalize(item["name"]).split(":")[-1] == target]
            if not exact:
                return _result("bypass", reason="explicit_skill_unavailable", explicit=target)
            if len(exact) != 1:
                return _result("ambiguous", reason="explicit_name_collision",
                               candidates=[_small(item) for item in exact[:limit]])
            if exact[0]["id"] not in {m["id"] for m in matches}:
                matches.append(exact[0])
        if len(matches) > limit:
            return _result("ambiguous", reason="selection_limit_exceeded", required_count=len(matches))
        chosen = [_small(item, reason="explicit_name") for item in matches]
        return _result("use_skill", reason="exact_explicit_request", selected=chosen, candidates=chosen)

    pos_text = " ".join(positive)
    q, neg = features(pos_text), features(" ".join(negative))
    # Purpose/context is not automatically a second job: "find sources for an
    # essay", "make an image for a website". Preserve coordinated task objects.
    focused_parts = []
    for part in positive:
        focus = re.split(r"\b(?:for|fyrir|about|um)\b", part, maxsplit=1)[0]
        focused_parts.append(features(focus))
    focused_domains = set().union(*(part["domains"] for part in focused_parts))
    if focused_domains:
        q = {**q, "domains": frozenset(focused_domains)}
    for specific, parents in DOMAIN_PARENTS.items():
        if specific in q["domains"]:
            q = {**q, "domains": q["domains"] - parents}
    if "research" in q["domains"] and q["actions"] & {"search", "test"} and not q["actions"] & {"create", "edit"}:
        q = {**q, "domains": q["domains"] - {"document"}}
    # A requested language or nationality can describe the deliverable, for
    # example an image of an Icelandic village. It is not a second task.
    if q["domains"] & {"image", "website", "video", "presentation"} and q["actions"] & {"create", "design"}:
        if not q["terms"] & {"proofread", "proofreading", "grammar", "spelling", "málfræði", "stafsetning", "beyging"}:
            q = {**q, "domains": q["domains"] - {"language"}}
    # A verb from a different subtask must not make a read-only capability
    # eligible for a clause such as "create a PDF and review the code."
    change_scopes = [
        (part["domains"] & q["domains"], _compatible_actions(part["actions"]))
        for part in focused_parts
        if part["actions"] and part["actions"] <= _CHANGE_ACTIONS
    ]
    denied_domains = neg["domains"] - q["domains"]
    if not q["terms"] or not items:
        return _result("bypass", reason="no_positive_request_or_available_skill")
    # A complete supplied trigger may name a specialised operation outside the
    # built-in verb vocabulary. Only an exact multiword request gets this escape
    # hatch; a topical mention embedded in prose does not.
    direct_triggers = {normalize(t).strip() for item in items for t in item["triggers"]
                       if len(t.split()) >= 2 and normalize(t).strip() == pos_text.strip()}
    if not q["actions"] and not direct_triggers:
        return _result("bypass", reason="no_task_action: a topical mention is insufficient")

    profiles = [_profile(item["name"], item["description"], item["tags"], item["triggers"]) for item in items]
    specific_terms = q["terms"] - GENERIC_WORDS - STOP_WORDS
    frequency = Counter(term for p in profiles for term in specific_terms & p["terms"])
    compatible_actions = _compatible_actions(q["actions"])
    ranked = []
    for item, p in zip(items, profiles):
        if normalize(item["name"]) in denied_explicit or normalize(item["id"]) in denied_explicit:
            continue
        if p["primary"] & denied_domains and not p["primary"] & q["domains"]:
            continue
        shared_domains = q["domains"] & p["domains"] - denied_domains
        primary_matches = q["domains"] & p["primary"] - denied_domains
        if any(domains & (primary_matches or shared_domains) and not actions & p["actions"]
               for domains, actions in change_scopes):
            continue
        shared_actions = compatible_actions & p["actions"]
        overlap = specific_terms & p["terms"]
        anchors = specific_terms & p["anchors"]
        # Generic read/analyze/review may apply to a well-defined domain even
        # when an economical metadata description omits that verb.
        fallback_actions = q["actions"] & _DOMAIN_ACTION_FALLBACK
        if not primary_matches - _SOFT_DOMAINS:
            fallback_actions -= {"search"}
        action_ok = bool(shared_actions or (fallback_actions and primary_matches))
        trigger = next((t for t in p["triggers"] if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", pos_text)), None)
        lexical = sum(1 + min(1.6, math.log((len(items) + 1) / (frequency[t] + 1))) for t in overlap)
        strong_domains = primary_matches - _SOFT_DOMAINS
        # Name anchoring or two discriminative terms allows specialist skills
        # outside the authored domain list; popularity is never part of this gate.
        lexical_gate = (len(overlap) >= 2 and lexical >= 3.0) or (bool(anchors) and lexical >= 2.0)
        direct_trigger = trigger in direct_triggers if trigger else False
        eligible = (action_ok or direct_trigger) and bool(strong_domains or primary_matches or lexical_gate or trigger)
        if not p["domains"]:
            eligible = direct_trigger or bool(lexical_gate and (shared_actions or fallback_actions))
        if not eligible:
            continue
        # A domain claimed only deep in unrelated metadata needs further evidence.
        if not primary_matches and not (lexical_gate or trigger):
            continue
        score = (5.0 * len(primary_matches) + 1.2 * len(shared_domains - primary_matches)
                 + min(3.0, 0.7 * lexical) + min(1.2, 0.6 * len(shared_actions))
                 + min(1.5, 0.6 * len(anchors)) + (2.0 if trigger else 0.0))
        # A keyword-stuffed catch-all loses to a focussed skill on the same intent.
        score /= 1.0 + 0.12 * max(0, len(p["primary"]) - len(primary_matches) - 1)
        reason = {"domains": sorted(primary_matches or shared_domains),
                  "actions": sorted(shared_actions), "terms": sorted(overlap)[:8]}
        if trigger:
            reason["trigger"] = trigger
        ranked.append({"item": item, "profile": p, "score": score, "rank": score + _feedback(item),
                       "matched": primary_matches or shared_domains, "evidence": reason})
    ranked.sort(key=lambda row: (-row["rank"], row["item"]["id"]))
    public = [{**_small(row["item"]), "score": round(row["score"], 3), "evidence": row["evidence"]}
              for row in ranked[:limit]]
    if not ranked:
        return _result("bypass", reason="no_supported_match", candidates=public)

    # One winner per requested domain. Generic overlaps (e.g. "document" in a
    # PDF task) are subordinate to a more specific object and do not add intents.
    target_domains = set(q["domains"])
    if target_domains - _SOFT_DOMAINS:
        target_domains -= _SOFT_DOMAINS
    winners, ambiguous = [], False
    if target_domains:
        for domain in sorted(target_domains):
            relevant = [row for row in ranked if domain in row["matched"]]
            if not relevant:
                continue
            first = relevant[0]
            if len(relevant) > 1:
                second = relevant[1]
                margin = first["score"] - second["score"]
                if margin < max(1.15, first["score"] * 0.14):
                    ambiguous = True
            if first["item"]["id"] not in {w["item"]["id"] for w in winners}:
                winners.append(first)
    else:
        winners = ranked[:1]
        if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < 1.15:
            ambiguous = True
    if not winners:
        winners = ranked[:1]
    # Never present a partial answer as complete when separate requested domains
    # cannot be resolved. Secondary descriptive concepts are not additional jobs.
    unresolved = target_domains - set().union(*(row["matched"] for row in winners))
    distinct_jobs = len(positive) > 1 and len(target_domains) > 1
    if ambiguous:
        return _result("ambiguous", reason="close_semantic_matches: clarify the intended skill", candidates=public)
    if len(winners) > limit or (unresolved and distinct_jobs):
        return _result("ambiguous", reason="incomplete_multi_intent_selection", candidates=public,
                       unresolved_domains=sorted(unresolved), required_count=len(winners))
    selected = [_small(row["item"], reason="metadata_evidence") for row in winners]
    return _result("use_skill", reason="clear_metadata_match" if len(selected) == 1 else "multiple_distinct_intents",
                   selected=selected, candidates=public)
