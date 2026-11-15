"""Conservative bilingual extraction from historical user turns."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator

_INPUT_LIMIT = 100_000
_LESSON_LIMIT = 8
_WORKFLOW_LIMIT = 420

_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,5}\s+([^#]+?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d{1,2}[.)]\s+)(.+)$")
_QUOTED = re.compile(r"`+[^`\n]+`+|\"[^\"\n]+\"|(?<!\w)'[^'\n]+'(?!\w)|„[^“\n]+“|«[^»\n]+»|“[^”\n]+”")
_SENTENCE_BOUNDARY = re.compile(r"(?<=\?)\s+|(?<=[.!]) +(?=[A-ZÁÐÉÍÓÚÝÞÆÖ])")
_PASTED_CONTEXT = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*AGENTS\.md\b|</?(?:instructions|environment_context|"
    r"permissions_instructions|app-context|user-prompt|developer|system)>|"
    r"\[(?:/?INST|/?SYSTEM|/?ASSISTANT)\]|"
    r"(?:user|notandi|assistant|developer|system|tool|dæmi|example)\s*:)"
)
_ROLE_LINE = re.compile(r"^(?:user|notandi|aðstoðarmaður|assistant|developer|system|tool)\s*:", re.I)

_PRIVATE = re.compile(
    r"""(?ix)
    [\w.+-]+@[\w.-]+\.[a-z]{2,}                         # email
    |(?:https?|file|ssh|ftp)://|(?:[a-z]:[\\/]|\\\\|/home/|/users/|~/)  # path or URL
    |\b(?:gh[pousr]_|github_pat_|sk-(?:proj-)?|xox[bpors]-|AKIA|ASIA)[a-z0-9_-]{8,}
    |-----\s*begin\b.{0,30}\b(?:private|certificate)
    |\beyJ[a-z0-9_=-]{8,}\.[a-z0-9_=-]{4,}\.[a-z0-9_=-]{8,} # JWT
    |\b(?:bearer|basic)\s+[a-z0-9._=-]{8,}
    |\b[\da-f]{24,}\b|\b[a-z0-9_=-]{40,}\b                # raw hashes and tokens
    |\b(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|private[ _-]?key|
       password|passphrase|secret|lykilorð|api[ _-]?lykill|einkalykill|
       kennitala|kortanúmer|bankareikning|reikningsnúmer|innskráningarnúmer|
       viðskiptanúmer|símanúmer|heimilisfang|pin[ _-]?code|login[ _-]?id|
       client[ _-]?id|home[ _-]?address|account[ _-]?number|credit[ _-]?card|
       phone[ _-]?number|wallet[ _-]?address)\w*\b
    |(?<!\d)(?:\+?\d[ \t().–—-]*){5,}(?!\d)             # account/credential-like number groups
    |\b\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})\b|\b(?:19|20)\d{2}\b
    |(?-i:\b[A-ZÁÐÉÍÓÚÝÞÆÖ][a-záðéíóúýþæö-]+\s+[A-ZÁÐÉÍÓÚÝÞÆÖ][a-záðéíóúýþæö-]+(?:son|dóttir)\b)
    """
)
_COMMAND_LIKE = re.compile(
    r"(?i)(?:<\/?[a-z][^>]{0,70}>|!\[|\[\[|\[[^\]\n]{1,100}\]\(|\$\(|&&|\|\||[\\/][^ /]{2,}[\\/]|"
    r"(?:^|\s)(?:curl|wget|irm|invoke-webrequest|invoke-expression|powershell|"
    r"bash|cmd\.exe|sudo|sch(?:ed)?tasks|ssh|scp)\s+[-\w])"
)
_OPERATION = re.compile(
    r"(?i)\b(?:eyð[aiuð][\w]*|delete[sd]?|destroy|wipe|format\s+(?:the\s+)?(?:disk|drive)|"
    r"publish|deploy|upload|push|senda|sendu|transfer|wire\s+transfer|trade|"
    r"live[ _-]+(?:orders?|trades?|trading|accounts?|positions?|execution)|"
    r"(?:market|limit|stop)[ _-]+orders?|(?:place|submit|execute)[ _-]+(?:an?[ _-]+)?order|"
    r"raunviðskipt\w*|raunfærsl\w*|raunreikning\w*|viðskiptapantan\w*|"
    r"(?:opna|loka|framkvæma|keyra|virkja)\s+(?:viðskipt|pöntun|stöð)\w*|"
    r"(?:disable|skip|override|ignore)\s+(?:the\s+)?(?:safety|review|approval|confirmation)\w*|"
    r"(?:sleppa|hunsa|afvirkja)\s+(?:örygg|samþykk|staðfest|yfirferð)\w*|"
    r"framhjá|bypass|exfiltrat\w*|exploit\w*|attack\w*|smita\w*)\b"
)
_TRANSIENT = re.compile(
    r"\b(?:bara núna|í bili|í dag|á morgun|í þetta sinn|í þetta skipti|í þessari lotu|"
    r"fyrir þessa beiðni|í þessu tilviki|for now|only this time|today|tomorrow|for this turn|"
    r"for this request|right now|one[- ]off)\b",
    re.I,
)
_PROJECT = re.compile(
    r"\b(?:í (?:þessu|okkar) verkefni|fyrir þetta verkefni|í þessu forriti|"
    r"í þessum gagnagrunni|for this project|in this project|in this codebase|"
    r"for this repository|for our project)\b",
    re.I,
)
_DEICTIC = re.compile(
    r"\b(?:þessa skrá|þessi gögn|þennan hlekk|þetta dæmi|this file|this dataset|"
    r"this attachment|the above|the attached)\b",
    re.I,
)
_QUESTION = re.compile(
    r"^(?:(?:mun|mundi|myndi|er|eru|var|voru|má|mega|getur|getum|gæti|þarf|þurfa|"
    r"will|would|does|do|did|is|are|can|could|should|must)\s+(?:hann|hún|það|þú|ég|"
    r"við|þið|codex|ofsafaerni|he|she|it|you|i|we|they|this)\b|"
    r"(?:hvernig|hvenær|hvar|af hverju|hvers vegna|what|how|when|where|why)\b)",
    re.I,
)
_FACTUAL_ADVERB = re.compile(
    r"\b(?:always|never)\s+(?:reaches|reached|exceeds|exceeded|contains|returns|returned|"
    r"produces|produced|fails|failed|matches|falls|rose|arises|appears|stays|happens)\b"
    r"|\b(?:nær|náði|fer|fór|birtist|kemur|kom|stendur)\s+(?:alltaf|aldrei)\b",
    re.I,
)
_DURABLE = re.compile(
    r"\b(?:alltaf|aldrei|héðan í frá|framvegis|upp frá þessu|sjálfgefi[ðn]|"
    r"í öllum|fyrir öll|í framtíðinni|í hverri|í hvert sinn|í hvert skipti|við hverja|"
    r"always|never|by default|from now on|going forward|in all|for all|"
    r"every time|in the future|each time)\b",
    re.I,
)
_INTENT = re.compile(
    r"\b(?:ég (?:vil|kýs|óska eftir|bið um|hef frekar|mæli með|nota venjulega)|"
    r"mér (?:finnst betra|hentar betur|líkar betur)|mitt sjálfgefna|"
    r"i (?:prefer|always use|usually use|want you to)|my default)\b",
    re.I,
)
_DIRECT = re.compile(
    r"""(?ix)\b(?:
    þú\s+(?:átt|skalt|mátt|þarft|verður)|(?:codex|þú)\s+á\s+(?:að|alltaf)|
    skaltu|áttu|mundu|notaðu|nýttu|mótaðu|byrjaðu|hafðu|gerðu|skrifaðu|vistaðu|
    geymdu|skráðu|notið|athugaðu|staðfestu|sannreyndu|opnaðu|slepptu|
    forðastu|veldu|haltu|láttu|taktu|sýndu|settu|segðu|fylgdu|teldu|
    keyrðu|varðveittu|svaraðu|framlengdu|notum|byrjum|geymum|sannreynum|
    (?:codex|you)\s+(?:should|must)
    )\b"""
)
_ENGLISH_DIRECT = re.compile(
    r"^(?:(?:please|always|never|going forward|from now on|by default)[\s,]+)*"
    r"(?:use|start|avoid|skip|prefer|format|record|check|keep|include|omit|show|"
    r"make|run|save|write|verify|respond|answer)\b",
    re.I,
)
_ICELANDIC_RULE_START = re.compile(
    r"^(?:alltaf|aldrei|framvegis|héðan í frá)[ ,:]+(?:að\s+)?"
    r"(?:nota|nýta|lesa|taka|breyta|byrja|halda|skrifa|geyma|vista|skrá|keyra|sýna|velja|"
    r"sannreyna|staðfesta|svara|afhenda|skila)\b",
    re.I,
)
_CORRECTION = re.compile(
    r"^(?:leiðrétting(?:in)?\s*[:.,]|ég meinti\b|ég átti við\b|"
    r"(?:nei|rangt)\s*[,–.—:]|ekki svona\b|correction\s*[:.,]|"
    r"i meant\b|actually\s*,|no\s*[,–.—:]|that(?:'s| is) wrong\b)",
    re.I,
)
_CONTRAST = re.compile(r"\b(?:ekki|heldur|í stað|not|instead|rather than)\b", re.I)
_NO_DETAILS = re.compile(
    r"\b(?:það sem ég bað um|misskildir|þetta er rangt|þetta er ekki rétt|"
    r"þetta er ekki það|reyndu aftur|not what i (?:asked|meant)|you misunderstood|"
    r"try again|that is incorrect)\b",
    re.I,
)
_STYLE_CORRECTION = re.compile(
    r"^(?:ekki|don't|do not)\s+(?:nota|setja|bæta|blanda|skrifa|nefna|use|add|mix|write)\b"
    r".*\b(?:í (?:fyrirsögnum|fyrirsagnir|lokasvör(?:um)?|svör(?:um)?|skýrsl(?:um|ur)|leiðbeining(?:um|ar))|"
    r"in (?:headings|reports|final answers|responses|instructions))\b",
    re.I,
)
_GENERIC_INTENT = re.compile(
    r"^(?:ég (?:vil|kýs)|i prefer|i want you to)[\s:]+(?:þetta|það|it|this|that|"
    r"betra|better|meira|more|minna|less)\s*[.!?]?$",
    re.I,
)
_WORK_CONTEXT = re.compile(
    r"\b(?:codex|obsidian|markdown|html|pdf|excel|github|opencode|deepseek|hermes|cli|api|"
    r"code\w*|test\w*|workflow\w*|report\w*|heading\w*|output\w*|summar\w*|"
    r"update\w*|evidence|verification|format\w*|file\w*|document\w*|"
    r"kóð\w*|próf\w*|verkfer\w*|verkefn\w*|verklag\w*|skýrsl\w*|fyrirsagn\w*|"
    r"úttak\w*|samantekt\w*|staðfest\w*|sannreyn\w*|sönnun\w*|skrá\w*|"
    r"örygg\w*|tal\w*|eining\w*|töl\w*|svör\w*|svar\w*|íslensk\w*|ensk\w*|"
    r"icelandic|english)\b",
    re.I,
)

_PROCEDURE_HEADINGS = frozenset({"verkferill", "staðfest verklag", "verklag", "workflow", "procedure", "reusable workflow"})
_CHECK_HEADINGS = frozenset({"sannprófun", "staðfesting", "prófun", "verification", "checks", "validation"})
_PLANNED = re.compile(r"\b(?:næsta skref|á eftir að|áætlað|proposed|planned|todo|yet to|untested)\b", re.I)
_CHECK_RESULT = re.compile(r"\b(?:stóðst|staðfest|án villu|passed|verified|confirmed|succeeded|no errors)\b", re.I)
_FAILED_RESULT = re.compile(
    r"\b(?:mistókst|ekki sta[ðf]\w*|stóðst ekki|óstaðfest|failed|did not pass|not verified|"
    r"unconfirmed|never (?:passed|succeeded)|neither\b.{0,45}\bpassed)\b",
    re.I,
)


def _visible_lines(raw: str) -> Iterator[str]:
    """Remove pasted role wrappers, quoted Markdown and literal code blocks."""
    fence: str | None = None
    for line in raw.splitlines():
        if not line or line.startswith(("    ", "\t")):
            continue
        match = _FENCE.match(line)
        if match:
            delimiter = match.group(1)
            if fence is None:
                fence = delimiter
            elif delimiter.startswith(fence) and not line[match.end():].strip():
                fence = None
            continue
        if fence is not None or line.lstrip().startswith(">") or _ROLE_LINE.match(line.strip()):
            continue
        yield line.strip()


def _clean(value: str, *, max_length: int) -> str | None:
    text = unicodedata.normalize("NFC", value)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"})
    text = " ".join(text.split()).strip(" \t-*")
    if not 12 <= len(text) <= max_length or len(text.split()) < 3:
        return None
    if _PRIVATE.search(text) or _COMMAND_LIKE.search(text) or _OPERATION.search(text):
        return None
    if text.startswith(("#", "{", "[", "|", "`", "…")):
        return None
    # Preserve useful quoted technical terminology as plain text. Trigger
    # detection separately ignores quotations so pasted rules do not qualify.
    text = text.replace("`", "")
    return text.rstrip(" .!?").rstrip() + "."


def _uint(value: int) -> bool:
    return type(value) is int and 0 < value <= 10_000


def _without_asides(value: str) -> str:
    """Ignore parenthetical observations when locating directive markers."""
    visible = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if not depth:
                visible.append(" ")
        elif not depth:
            visible.append(char)
    return "".join(visible)


def _corrections(raw: str) -> Iterator[dict]:
    for line in _visible_lines(raw):
        # Check private data before sentence splitting so punctuation inside
        # emails, URLs, tokens or numeric identifiers cannot split the match.
        if _PRIVATE.search(line) or _COMMAND_LIKE.search(line) or _OPERATION.search(line):
            continue
        item = _ITEM.match(line)
        for sentence in _SENTENCE_BOUNDARY.split(item.group(1) if item else line):
            text = _clean(sentence, max_length=340)
            if text is None or _TRANSIENT.search(text) or _DEICTIC.search(text) or _GENERIC_INTENT.match(text):
                continue
            unquoted = _QUOTED.sub(" ", sentence)
            if "?" in unquoted or _QUESTION.match(unquoted):
                continue
            semantic = _FACTUAL_ADVERB.sub(" ", _without_asides(unquoted)).strip()
            if len(semantic.split()) < 3:
                continue
            durable = _DURABLE.search(semantic) is not None
            intent_match = _INTENT.search(semantic)
            intent = bool(
                intent_match and len(semantic[intent_match.end():].strip(" .!?")) >= 3
                and (durable or _WORK_CONTEXT.search(semantic))
            )
            direct = bool(
                _DIRECT.search(semantic) or _ENGLISH_DIRECT.match(semantic) or _ICELANDIC_RULE_START.match(semantic)
            )
            if _PROJECT.search(unquoted):
                scope = "project"
            elif durable or intent:
                scope = "global"
            else:
                scope = "session"
            if (durable and direct) or intent:
                yield {"kind": "preference", "text": text, "scope": scope, "evidence": "user-stated preference"}
                continue
            anchored = _CORRECTION.match(semantic)
            project_prefix = _PROJECT.match(semantic)
            style = semantic[project_prefix.end():].lstrip(" ,:-") if project_prefix else semantic
            if _STYLE_CORRECTION.match(style):
                yield {"kind": "correction", "text": text,
                       "scope": "project" if scope == "project" else "global", "evidence": "user-stated correction"}
            elif anchored and not _NO_DETAILS.search(semantic):
                substance = semantic[anchored.end():].strip(" ,:.–—")
                if len(substance.split()) >= 3 and (_CONTRAST.search(substance) or direct or anchored.group().endswith(":")):
                    yield {"kind": "correction", "text": text, "scope": scope, "evidence": "user-stated correction"}


def _workflow(raw: str, verified_checks: int) -> dict | None:
    if not _uint(verified_checks) or _PASTED_CONTEXT.search(raw):
        return None
    groups: dict[str, list[str]] = {"procedure": [], "checks": []}
    section = None
    for line in _visible_lines(raw):
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1).strip(" :").casefold()
            if title in _PROCEDURE_HEADINGS:
                section = "procedure"
            elif title in _CHECK_HEADINGS:
                section = "checks"
            else:
                section = None
            continue
        item = _ITEM.match(line)
        if section is None or item is None:
            continue
        piece = _clean(item.group(1), max_length=135)
        if piece is None or _TRANSIENT.search(piece) or _PLANNED.search(piece) or _FAILED_RESULT.search(piece):
            # A missing step or check may change the meaning of a workflow.
            return None
        if len(groups[section]) >= (4 if section == "procedure" else 2):
            return None
        groups[section].append(piece)
    if len(groups["procedure"]) < 2 or not any(_CHECK_RESULT.search(piece) for piece in groups["checks"]):
        return None
    content = "Verkferill. " + " ".join(groups["procedure"]) + " Sannprófun. " + " ".join(groups["checks"])
    if len(content) > _WORKFLOW_LIMIT:
        return None
    return {
        "kind": "workflow",
        "text": content,
        "scope": "project",
        "evidence": "structured procedure with recorded checks",
    }


def find_lessons(user: str, assistant: str = "", *, verified_checks: int = 0) -> list[dict]:
    """Find short user-authored lessons and checks-supported workflow candidates."""
    if not isinstance(user, str) or not user.strip() or len(user) > _INPUT_LIMIT or _PASTED_CONTEXT.search(user):
        return []
    unique: dict[str, dict] = {}
    for lesson in _corrections(user):
        unique.setdefault(lesson["text"].casefold(), lesson)
        if len(unique) >= _LESSON_LIMIT:
            break
    if isinstance(assistant, str) and len(assistant) <= _INPUT_LIMIT and len(unique) < _LESSON_LIMIT:
        workflow = _workflow(assistant, verified_checks)
        if workflow:
            unique.setdefault(workflow["text"].casefold(), workflow)
    return list(unique.values())
