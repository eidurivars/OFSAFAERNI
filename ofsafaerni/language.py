"""Bounded, local-only Icelandic/English language features.

The small bilingual vocabulary is authored for routing, not a general translator.
If Miðeind's ``islenska`` is installed in this Python environment, BÍN adds
possible lemmas without picking one ambiguous analysis as ground truth. No
packages, models, or dictionaries are downloaded at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import re
import unicodedata


TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
STOP_WORDS = frozenset("""
a an the this that these those i me my mine you your we our us it its he she they
their them in on at to for of from by with and or but as be am is are was were
been being have has had can could would should will shall may might must do does
did please help need want use using get make some any all then also about into
through out up down just only let lets let's don't not without no than more
ég mig mér mín minn mínar þú þig þér þinn við okkur okkar þið ykkur ykkar hann
hún það þetta þessi þessir þessi þessar þessum þess þessara þetta þau þeirra
og eða en að af á í til frá fyrir með um sem svo líka síðan bara aðeins ekki
án nei er eru var voru vera verið væri má mega megi getur getum gæti gætu
vilt vil vilja viljum þarf þurfa þarfir skal skalt skaldu láttu látið vinsamlegast
hjálpa hjálpaðu hjálpið nokkur nokkrar nýtt nýja nýjan nýjar eitt eina einn
þaðan þar hér hvort hvernig hvers vegna hvenær
""".split())

# Explicit paradigms avoid unsafe suffix-stripping (e.g. unrelated words sharing
# a short stem). BÍN broadens this when available; the fallback stays predictable.
_FAMILIES = {
    "skjal": "skjal skjalið skjals skjalsins skjalinu skjali skjöl skjölin skjölum skjölunum skjalanna skjala",
    "tafla": "tafla töflu taflan töfluna töflunni töflunnar töflur töflurnar töflunum taflna",
    "töflureiknir": "töflureiknir töflureikni töflureiknis töflureikninum töflureikninn töflureikna töflureiknum töflureiknar",
    "vinnubók": "vinnubók vinnubókin vinnubókina vinnubókinni vinnubókar vinnubækur vinnubækur vinnubókum vinnubókunum",
    "glæra": "glæra glæru glæran glæruna glærunni glærur glærurnar glærum glærunum glæranna",
    "kynning": "kynning kynningin kynningu kynninguna kynningunni kynningar kynningarinnar kynningum",
    "mynd": "mynd myndin myndina myndinni myndar myndarinnar myndir myndirnar myndum myndunum myndanna",
    "vefsíða": "vefsíða vefsíðu vefsíðan vefsíðuna vefsíðunni vefsíðunnar vefsíður vefsíðurnar vefsíðum vefsíðunum",
    "vefur": "vefur vef vefur vefinn vefnum vefs vefsins vefir vefjum vefja",
    "vafri": "vafri vafra vafrann vafranum vafrar vöfrum vöfrunum",
    "kóði": "kóði kóðinn kóða kóðann kóðanum kóðans kóðar kóðum kóðunum",
    "kóðasafn": "kóðasafn kóðasafnið kóðasafni kóðasafninu kóðasafns kóðasafnsins kóðasöfn kóðasöfnin kóðasöfnum",
    "færni": "færni færnin færnina færninni færnir færniskrá færniskráin færniskránni færniskrár færniskránna sérfærni",
    "verklag": "verklag verklagið verklagi verklaginu verklags verklög",
    "uppfæra": "uppfæra uppfærðu uppfærið uppfærir uppfært uppfæra uppfærsla uppfærslu",
    "rýna": "rýna rýndu rýnið rýnir rýnt rýni",
    "aðgangsheimild": "aðgangsheimild aðgangsheimildina aðgangsheimildar aðgangsheimildir aðgangsheimildum",
    "forrit": "forrit forritið forriti forritinu forrits forritsins forritin forritum forritunum",
    "gagnagrunnur": "gagnagrunnur gagnagrunn gagnagrunninn gagnagrunninum gagnagrunns gagnagrunnar gagnagrunna gagnagrunnum",
    "tölvupóstur": "tölvupóstur tölvupóst tölvupóstinn tölvupóstinum tölvupósts tölvupóstar tölvupósta tölvupóstum",
    "dagatal": "dagatal dagatalið dagatali dagatalinu dagatals dagatöl dagatölin dagatölum dagatalanna",
    "hlutabréf": "hlutabréf hlutabréfið hlutabréfi hlutabréfinu hlutabréfs hlutabréfsins hlutabréfa hlutabréfum hlutabréfunum hlutabréfin",
    "fundargerð": "fundargerð fundargerðin fundargerðina fundargerðinni fundargerðar fundargerðir fundargerðum fundargerðunum fundargerðanna",
    "heimild": "heimild heimildin heimildina heimildinni heimildar heimildir heimildirnar heimilda heimildum heimildunum frumheimild frumheimildin frumheimildina frumheimildir frumheimildirnar frumheimildum frumheimildunum",
    "tilvísun": "tilvísun tilvísunin tilvísunina tilvísuninni tilvísunar tilvísanir tilvísanirnar tilvísunum tilvísununum",
    "rannsókn": "rannsókn rannsóknin rannsóknina rannsókninni rannsóknar rannsóknarinnar rannsóknir rannsóknirnar rannsóknum rannsóknunum rannsókna",
    "viðskiptastefna": "viðskiptastefna viðskiptastefnan viðskiptastefnu viðskiptastefnuna viðskiptastefnunni viðskiptastefnur viðskiptastefnum viðskiptaregla viðskiptareglan viðskiptareglu viðskiptaregluna viðskiptareglur viðskiptareglurnar viðskiptareglum",
    "herma": "herma hermdu hermið hermun hermunin hermunina hermuninni hermunar hermunum",
    "keyra": "keyra keyrðu keyrið keyri keyrir keyrsla keyrslu keyrslur keyrslum",
    "færa": "færa færðu færið færi færir flytja flyttu flytjið flytji flytur",
    "stilla": "stilla stilltu stillið stilli stillir aðlaga aðlagaðu aðlagið aðlagi aðlagar",
    "athuga": "athuga athugaðu athugið athugi athugar kanna kannaðu kannið kanni kannar",
    "byggja": "byggja byggðu byggið byggi byggir bygging byggingu",
    "lendingarsíða": "lendingarsíða lendingarsíðan lendingarsíðu lendingarsíðuna lendingarsíðunni lendingarsíður lendingarsíðurnar lendingarsíðum lendingarsíðunum",
    "íslenska": "íslenska íslenskan íslenskar íslenski íslenskt íslensku íslenskum íslenskir íslenskra íslensks íslenskrar íslenskur",
    "stafsetningarvilla": "stafsetningarvilla stafsetningarvillan stafsetningarvillu stafsetningarvilluna stafsetningarvillunni stafsetningarvillur stafsetningarvillurnar stafsetningarvillum stafsetningarvillunum",
    "beyging": "beyging beygingin beygingu beyginguna beygingunni beygingar beygingarinnar beygingar beygingarnar beygingum beygingunum fallbeyging fallbeygingin fallbeygingu fallbeyginguna fallbeygingunni fallbeygingar fallbeygingum",
    "graf": "graf grafið grafin grafi grafinu gröf gröfin gröfum grafanna verðgraf verðgrafið verðgrafi verðgrafinu verðgrafs verðgröf",
    "súlurit": "súlurit súluritið súluriti súluritinu súlurita súluritum línurit línuritið línuriti línuritinu skífurit skífuritið skífuriti skífuritinu",
    "bakprófa": "bakprófa bakprófaðu bakprófið bakprófi bakprófun bakprófunin bakprófunina bakprófunum bakpróf bakprófið bakprófa bakprófum",
    "teikna": "teikna teiknaðu teiknið teikni teiknar",
    "skýrsla": "skýrsla skýrslu skýrslan skýrsluna skýrslunni skýrslunnar skýrslur skýrslurnar skýrslum skýrslunum skýrslna",
    "ritgerð": "ritgerð ritgerðin ritgerðina ritgerðinni ritgerðir ritgerðunum ritgerða ritgerðum",
    "þýða": "þýða þýddu þýðið þýði þýðir þýðing þýðingu þýðingar þýðingarinnar þýðingin þýðinguna þýðingunni",
    "búa": "búa búðu búið býr býrðu skapa skapaðu smíða smíðaðu smíði útbúa útbúðu útbúið útbúi útbýr",
    "gera": "gera gerðu gerið geri gerir gerum",
    "skrifa": "skrifa skrifaðu skrifið skrifað skrifar skrifi rita ritaðu semja semdu",
    "lesa": "lesa lestu lesið les lesi lesum",
    "greina": "greina greindu greinið greini greining greiningu greiningar greininguna greiningunni",
    "breyta": "breyta breyttu breytið breyti breyting breytingu breytingar breytingum",
    "sameina": "sameina sameinaðu sameinið sameini sameining sameiningu sameiningar sameinar",
    "skipta": "skipta skiptu skiptið skipti skipting skiptingu",
    "þjappa": "þjappa þjappaðu þjappið þjöppun þjöppunina þjöppunar",
    "draga": "draga dragðu dragið dregið dregur",
    "sækja": "sækja sæktu sækið sæki sækir",
    "skoða": "skoða skoðaðu skoðið skoði skoðar skoðun skoðunar",
    "yfirfara": "yfirfara yfirfarðu yfirfarið yfirferð yfirferðar yfirferðina yfirferðinni yfirfari yfirfer",
    "laga": "laga lagaðu lagið lagfærðu lagfæra lagfærið lagfæring lagfæringu leiðrétta leiðréttu leiðréttið",
    "prófa": "prófa prófaðu prófið prófun prófunar prófanir prófanirnar prófunum",
    "hanna": "hanna hannaðu hannið hönnun hönnunina hönnunar hönnuninni",
    "finna": "finna finndu finnið finnur leita leitaðu leitið leiti leit leitin leitina leitar",
    "reikna": "reikna reiknaðu reiknið reikni reiknar útreikningur útreikninga útreikning útreikningi útreikningum",
    "setja": "setja settu setjið setji setur",
    "bæta": "bæta bættu bætið bæti bætir",
    "fjarlægja": "fjarlægja fjarlægðu fjarlægið fjarlægi fjarlægir eyða eyddu eyðið",
    "vista": "vista vistaðu vistið visti vistar vistun",
    "birta": "birta birtu birtið birti birtir birting birtingu",
    "senda": "senda sendu sendið sendi sendir",
    "bera": "bera berðu berið beri berum",
    "skoðun": "endurskoða endurskoðaðu endurskoðun endurskoðunar",
    "öryggi": "öryggi öryggis öryggið örygginu öryggisgalla öryggisgallar öryggisgöllum öryggisgöllunum",
    "aðgengi": "aðgengi aðgengis aðgengið aðgenginu aðgengilegt aðgengilega",
    "stafsetning": "stafsetning stafsetningu stafsetningar stafsetninguna stafsetningunni stafsetningarvillur stafsetningarvillurnar",
    "málfræði": "málfræði málfræðina málfræðinni málfræðilegar málfræðivillur málfræðivillurnar málfar málfari málfarið málfars",
}
_FORMS = {form: lemma for lemma, forms in _FAMILIES.items() for form in forms.split()}

# Concepts describe objects. Actions are separate so a mere topical mention
# ("I like PDF") cannot by itself invoke a task skill.
DOMAIN_WORDS = {
    "pdf": "pdf pdfs acrobat",
    "spreadsheet": "spreadsheet spreadsheets excel xlsx xls csv tsv workbook workbooks töflureiknir vinnubók",
    "document": "document documents docx word skjal ritgerð manuscript manuscripts",
    "presentation": "presentation presentations powerpoint pptx slides slide deck decks glæra kynning",
    "image": "image images imagegen picture pictures photo photos photograph photographs illustration illustrations portrait portraits logo logos mynd ljósmynd teikning raster",
    "website": "website websites webpage webpages frontend html css landing vefsíða vefur heimasíða heimasíðu lendingarsíða vefviðmót vefviðmóti vefviðmótið",
    "browser": "browser browsers playwright selenium vafri browsing",
    "code": "code coding codebase codebases programming program programs software python javascript typescript function functions kóði kóðasafn forrit forritun forritunar frumkóði frumkóða",
    "code_architecture": "graphify",
    "agent_skill": "færniskrá færniskrár",
    "security": "security secure vulnerability vulnerabilities authorization authentication öryggi aðgangsstýring aðgangsstýringu aðgangsheimild",
    "repository": "repository repositories github git repo repos commit commits pullrequest",
    "database": "database databases sql sqlite postgres postgresql mysql gagnagrunnur",
    "translation": "translate translating translation translations þýða",
    "language": "grammar spelling proofreading proofread icelandic typo typos inflection inflections íslenska málfræði stafsetning stafsetningarvilla beyging",
    "research": "research sources citations citation cite cited study studies evidence bibliography literature heimild tilvísun rannsókn",
    "email": "email emails gmail outlook tölvupóstur",
    "calendar": "calendar calendars schedule schedules meeting meetings appointment appointments dagatal fundur fundi fundinn fundar",
    "meeting_notes": "fundargerð",
    "audio": "audio sound speech recording recordings podcast podcasts hljóð hljóði hljóðið hljóðupptaka hljóðupptöku",
    "video": "video videos movie movies mp4 myndband myndbandið myndbönd myndbands myndbandi myndbandinu",
    "finance": "finance financial budget budgets budgeting expense expenses revenue banking fjármál fjármálum fjárhagsáætlun fjárhagsáætlunina útgjöld útgjöldin útgjöldum",
    "trading": "trading stocks stock shares equity equities forex options portfolio hlutabréf viðskiptastefna viðskiptaaðferð viðskiptaaðferðir",
    "backtest": "backtest backtests backtesting replay bakprófa",
    "technical": "resistance stuðningur stuðningi stuðning viðnám viðnámi viðnáms",
    "visualization": "visualize visualization visualisation chart charts plot plots graph graphs graf súlurit",
    "archive": "archive archives zip gzip tar",
    "accessibility": "accessibility wcag aria aðgengi",
    "test": "test tests testing unittest pytest regression prófa",
    "design": "design typography layout ux ui hanna",
}

ACTION_WORDS = {
    "create": "create creating generate generating build building make making produce producing compose composing draft drafting prepare preparing write writing draw drawing teikna búa gera skrifa byggja",
    "edit": "edit editing modify modifying change changing update updating revise revising format formatting add adding adjust adjusting polish polishing import importing reshape reshaping redesign redesigning breyta bæta færa stilla uppfæra",
    "read": "read reading inspect inspecting open opening view viewing skoða lesa",
    "analyze": "analyze analysing analyze analyzing analyse analysis audit auditing investigate investigating mark marking annotate annotating greina skoðun",
    "review": "review reviewing proofread proofreading yfirfara rýna",
    "install": "install installing installation installs installer",
    "fix": "fix fixing debug debugging repair repairing correct correcting resolve resolving laga",
    "merge": "merge merging combine combining join joining concatenate sameina",
    "split": "split splitting separate separating skipta",
    "extract": "extract extracting parse parsing pull retrieve retrieving list listing sækja",
    "convert": "convert converting conversion export exporting transform transforming umbreyta umbreyttu umbreyting umbreytingu umrita umritaðu",
    "compress": "compress compressing compression shrink shrinking þjappa",
    "translate": "translate translating translation þýða",
    "search": "search searching find finding research researching look lookup finna",
    "calculate": "calculate calculating recalculate recalculating compute computing sum summing formula formulas formulae reikna formúla formúlur formúlum",
    "schedule": "schedule scheduling reschedule rescheduling book booking arrange arranging schedule bóka bókaðu bókið skipuleggja skipuleggðu skipuleggið",
    "send": "send sending senda",
    "publish": "publish publishing deploy deploying host hosting birta",
    "test": "test testing automate automating backtest backtesting replay replaying simulate simulating validate validating verify verifying check checking trace tracing run running execute executing prófa bakprófa herma keyra athuga sannreyna sannreyndu sannreynið sjálfvirknivæða sjálfvirknivæddu",
    "visualize": "visualize visualizing visualise visualising plot plotting chart charting",
    "design": "design designing hanna",
    "compare": "compare comparing comparison compare bera samanburður samanburð",
    "remove": "remove removing delete deleting erase erasing fjarlægja",
    "save": "save saving store storing vista",
    "summarize": "summarize summarizing summarise summarising summary samantekt samantektina draga",
}

DOMAIN_PHRASES = {
    "spreadsheet": ("google sheets", "google töfl", "reikni töflu"),
    "website": ("web page", "web site", "landing page", "build a site"),
    "code": ("source code", "source file", "pull request", "programming language"),
    "code_architecture": ("code architecture", "file relationships", "code structure", "tengsl kóða"),
    "agent_skill": ("reusable agent workflow", "agent skill", "codex skill", "skill-creator", "skill-installer", "skill.md"),
    "image": ("transparent background", "remove background", "búa til mynd"),
    "language": ("check the spelling", "correct the grammar", "laga málfar"),
    "technical": ("technical analysis", "technical-analysis", "moving average", "hlaupandi meðalt", "stock chart", "price chart"),
    "meeting_notes": ("meeting notes", "meeting-notes", "meeting minutes", "meeting transcript", "action items", "fundargerð"),
}

# Productive compound recognition is limited to unambiguous domain heads and
# full attested fallback forms. We do not cut arbitrary word endings. The
# original token remains available, and compound prefixes that are themselves
# known words contribute their meaning (hlutabréfa + grafinu).
_COMPOUND_HEADS = frozenset({"kóði", "gagnagrunnur", "hlutabréf", "töflureiknir", "vinnubók",
                           "vefsíða", "dagatal", "fundargerð", "súlurit", "graf", "rannsókn",
                           "stafsetningarvilla", "beyging"})
_COMPOUND_FORMS = tuple(sorted(((form, lemma) for form, lemma in _FORMS.items()
                                if lemma in _COMPOUND_HEADS and len(form) >= 4),
                               key=lambda pair: (-len(pair[0]), pair[0])))

# Specific domains dominate their broader parent topics. This prevents a stock
# chart request selecting both a stock analyst and a general chart creator.
DOMAIN_PARENTS = {"technical": frozenset({"trading", "visualization"}),
                  "backtest": frozenset({"trading", "research", "test"}),
                  "code_architecture": frozenset({"code"}),
                  "agent_skill": frozenset({"repository"}),
                  "meeting_notes": frozenset({"calendar"}),
                  "translation": frozenset({"language"})}

GENERIC_WORDS = frozenset("""
task tasks work works working tool tools skill skills support supports suitable
file files data content text texts output outputs input inputs user users local
when including include includes available request requests project projects
result results manage managing management process processing information
format formats formatting format page pages report reports table tables
assistant assistants best better simple new existing need help use using create
make build generate read edit analyze review design test search find fix write
document documents skjal skrá skrána skránni skrár skrárnar gögnum gögn gögnin
texti texta textann verkefni verkefnið verkefna vinna vinnu vinna verk verkfæri
færni færnin færni skrár skýrsla tafla búa gera skrifa lesa skoða greina yfirfara
laga hanna finna prófa reikna setja bæta breyta draga sækja
""".split())


@lru_cache(maxsize=1)
def _bin():
    try:
        from islenska import Bin
        return Bin()
    except (ImportError, OSError, RuntimeError):
        return None


def runtime_info() -> dict:
    """Report the actual active local language engine, never a cloud service."""
    engine = _bin()
    result = {"engine": "builtin+bin" if engine is not None else "builtin", "local_only": True}
    if engine is not None:
        try:
            result["islenska_version"] = version("islenska")
        except PackageNotFoundError:
            result["islenska_version"] = "unknown"
    return result


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("’", "'")


@lru_cache(maxsize=8192)
def lemmas(word: str) -> frozenset[str]:
    """Preserve all bounded analyses, including the original word."""
    result = {word, _FORMS.get(word, word)}
    for form, lemma in _COMPOUND_FORMS:
        if len(word) >= len(form) + 3 and word.endswith(form):
            prefix = word[:-len(form)]
            if lemma == "kóði" and prefix in {"póst", "lita", "vöru", "strikamerkis"}:
                continue
            result.add(lemma)
            if prefix in _FORMS:
                result.add(_FORMS[prefix])
            break
    for prefix in ("endur", "endurskoð"):
        tail = word[len(prefix):] if word.startswith(prefix) else ""
        if tail in _FORMS:
            result.add(_FORMS[tail])
    engine = _bin()
    if engine is not None and len(word) <= 64 and word.isalpha():
        try:
            entries = engine.lookup(word)[1]
            for entry in entries[:24]:
                lemma = normalize(entry.ord).replace("-", "")
                result.add(lemma)
                result.add(_FORMS.get(lemma, lemma))
        except (LookupError, ValueError, OSError, RuntimeError):
            # Optional morphology cannot prevent the deterministic router working.
            pass
    # Safe, limited English plural recovery; never stem Icelandic arbitrary tails.
    if word.isascii() and len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        result.add(word[:-1])
    return frozenset(result)


_DOMAIN_INDEX = {word: concept for concept, words in DOMAIN_WORDS.items() for word in words.split()}
_ACTION_INDEX: dict[str, set[str]] = {}
for _action, _words in ACTION_WORDS.items():
    for _word in _words.split():
        _ACTION_INDEX.setdefault(_word, set()).add(_action)


def features(text: str) -> dict[str, frozenset[str]]:
    """Tokenize hyphenated compounds and collect bounded bilingual evidence."""
    normalized = normalize(text)
    tokens = TOKEN_RE.findall(normalized)
    terms: set[str] = set()
    domains: set[str] = set()
    actions: set[str] = set()
    for token in tokens[:1000]:
        if len(token) > 80:
            continue
        if token.endswith("'s"):
            token = token[:-2]
        # Function words have irrelevant homographic analyses in BÍN (e.g.
        # conjunction "sem" can analyse as the verb semja). Preserve their
        # literal action meaning where applicable, but never expand them.
        family = frozenset({token}) if token in STOP_WORDS else lemmas(token)
        terms.update(w for w in family if w not in STOP_WORDS and len(w) > 1)
        domains.update(_DOMAIN_INDEX[w] for w in family if w in _DOMAIN_INDEX)
        for word in family:
            actions.update(_ACTION_INDEX.get(word, ()))
    for domain, phrases in DOMAIN_PHRASES.items():
        if any(phrase in normalized for phrase in phrases):
            domains.add(domain)
    if {"codex", "verklag"} <= terms:
        domains.add("agent_skill")
    if terms & {"skill", "skills", "færni"} and terms & {"agent", "codex", "hermes", "github", "reusable"}:
        domains.add("agent_skill")
    if terms & {"codebase", "codebases", "kóðasafn"} and terms & {
        "architecture", "structure", "dependency", "dependencies", "relationship", "relationships",
        "tengjast", "tengsl", "uppbygging",
    }:
        domains.add("code_architecture")
    if {"trading", "visualization"} <= domains:
        domains.add("technical")
    if "trading" in domains and terms & {"simulate", "simulating", "simulation", "herma", "hermun"}:
        domains.add("backtest")
    # Familiar homonyms are not evidence for the technical domain.
    if re.search(r"\bstock\s+(?:car|cars|cube|cubes|broth)\b", normalized) and not terms & {"shares", "stocks", "trading", "equity"}:
        domains.discard("trading")
        terms.discard("stock")
    if re.search(r"\bpython\s+(?:snake|snakes)\b", normalized) and not terms & {"code", "programming", "software"}:
        domains.discard("code")
        terms.discard("python")
    if re.search(r"\bpdf[\s\-–‑]+(?:printer\w*|prent\w*)\b", normalized):
        domains.discard("pdf")
        terms.discard("pdf")
    if "technical" in domains and "trading" not in domains and not re.search(r"(?:technical[ -]analysis|price chart|verðgraf|hlaupandi meðalt)", normalized):
        domains.discard("technical")
    # Common paraphrases have more meaning together than their individual words.
    if re.search(r"\b(?:turn|put)\b.{0,45}\binto\b", normalized):
        actions.add("convert")
    if re.search(r"\b(?:setja|settu|setjið)\b.{0,55}\b(?:dagatal|dagatalið|dagatali)\b", normalized):
        actions.add("schedule")
    if re.search(r"\b(?:put|add)\b.{0,80}\b(?:calendar|appointment|meeting)\b", normalized):
        actions.add("schedule")
    if "calendar" in domains and "færa" in terms:
        actions.add("schedule")
    if re.search(r"\b(?:setja|settu|setjið)\b.{0,55}\bupp\b", normalized):
        actions.add("install" if "agent_skill" in domains else "edit")
    if re.search(r"\b(?:fara|farðu|farið)\b.{0,20}\byfir\b", normalized):
        actions.add("review")
    if re.search(r"\b(?:taka|taktu|takið)\b.{0,80}\b(?:út|saman|fram)\b", normalized):
        actions.add("extract" if "út" in tokens or "fram" in tokens else "summarize")
    if re.search(r"\b(?:draga|dragðu|dragið)\b.{0,30}\b(?:út|saman)\b", normalized):
        actions.add("extract" if "út" in tokens else "summarize")
    if re.search(r"\b(?:búa|búðu|búið)\s+til\b", normalized):
        actions.add("create")
    if re.search(r"\b(?:láta|láttu|látið)\b.{0,100}\btilbú\w*", normalized):
        actions.add("create")
    if "technical" in domains and re.search(r"\b(?:hvar|what|where)\b", normalized):
        actions.add("analyze")
    return {"terms": frozenset(terms), "domains": frozenset(domains), "actions": frozenset(actions)}
