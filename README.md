# OFSAFAERNI

OFSAFAERNI er staðbundið færnikerfi fyrir Codex og Hermes. Það safnar `SKILL.md`-færniskrám, finnur viðeigandi færni fyrir íslenskar og enskar beiðnir og breytir staðfestu verklagi í drög að nýrri færni.

| Verk | Skipun |
| --- | --- |
| Safna staðbundnum færniskrám eða skoða opinbert GitHub-safn | `collect` |
| Skoða færni, staðfesta tiltekna útgáfu og taka aftur samþykki | `review`, `approve`, `revoke` |
| Velja færni fyrir beiðni eða hvert undirverk | `route`, `assign` |
| Skrá hvort valin færni nýttist | `feedback` |
| Búa til drög að færni úr unnu og sannreyndu verki | `learn` |
| Flytja yfirfarið færnisafn í Hermes eða annað Agent Skills-umhverfi | `export` |

## Byrja

Python 3.11 eða nýrri. Keyrðu úr rót geymslunnar. Viðbótin `icelandic` nýtir orðmyndir úr BÍN í gegnum `islenska` frá Miðeind. Færnivalið er staðbundið og þarf hvorki reikning né líkanaþjónustu.

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install ".[icelandic]"
./.venv/Scripts/python.exe -m ofsafaerni collect --root examples/demo-skills
./.venv/Scripts/python.exe -m ofsafaerni list --all
```

Á macOS og Linux er ræsirinn `./.venv/bin/python`. Dæmin hér fyrir neðan nota `ofsafaerni` úr virku sýndarumhverfi. Allar skipanir skila JSON. Notaðu `--db SLÓÐ` **fyrir** undirskipunina til að velja aðskilda færniskrá. Sjálfgefin skrá er undir `%LOCALAPPDATA%/OFSAFAERNI` á Windows og í `$XDG_STATE_HOME/ofsafaerni` eða `~/.local/state/ofsafaerni` annars staðar.

```text
ofsafaerni review ID
ofsafaerni approve ID --sha256 GATSUMMA_UR_REVIEW
ofsafaerni route "Dragðu saman helstu niðurstöðurnar úr PDF-skjalinu."
ofsafaerni load ID
```

`approve` tekur við heildargátsummu þeirrar útgáfu sem var yfirfarin. Ef samþykkt staðbundin færni breytist þarf að yfirfara og samþykkja nýja útgáfu. `route` skilar ástæðum ásamt vali, óvissu eða niðurstöðu um að sleppa vali. `$pdf-reading` er dæmi um að nefna færni beint.

Safnaðu úr venjulegum staðbundnum möppum. Hlekkir í skráakerfinu og OneDrive-staðgenglar geta komið í veg fyrir yfirferð heildarsafnsins.

### Söfnun og undirverk

```text
ofsafaerni collect --codex --project .
ofsafaerni collect --hermes
ofsafaerni collect --root /slod/ad/eigin/faerni
ofsafaerni collect --github OWNER/REPO --ref TAG --limit 20
ofsafaerni assign examples/subtasks.json
ofsafaerni feedback ID success
```

GitHub-söfnun tekur við opinberu safni og festir útgáfuna við commit. Hún geymir færniskrána til yfirferðar. Til að samþykkja slíka færni til notkunar skaltu sækja viðkomandi commit, safna úr staðbundnu eintaki og yfirfara fylgiskrárnar. `assign` tekur við undirverkum með `id`, `task` og valfrjálsu `agent`. Úttakið er úthlutun sem Codex eða annar stýrifulltrúi getur nýtt. Endurgjöf hefur afmörkuð áhrif á röðun þegar beiðni og færni eiga þegar saman.

### Læra af verkefni

```text
ofsafaerni learn examples/workflow.json --out ./drafts
ofsafaerni collect --root ./drafts
ofsafaerni list --all
```

Láttu `task`, `steps`, `checks` og `evidence` lýsa endurnýtanlega hluta þess sem reyndist virka. `learn` vistar `SKILL.md` og staðbundna upprunakvittun. Skoðaðu drögin með `review` og samþykktu nákvæma útgáfu með `approve`. Tólið les aðeins tilgreinda inntaksskrá.

### Codex

Þegar verkefnið er opnað í Codex fylgir færni fyrir söfnun, val og lærdóm. Til að fá val á hverri nýrri beiðni skaltu keyra `ofsafaerni hook-config` úr uppsetta Python-umhverfinu. Settu prentaða stillingu í `.codex/hooks.json` í því verkefni sem á að nota færnivalið. Ef skráin er þegar til skaltu bæta færslunni við `UserPromptSubmit` og halda gildandi krókum. Yfirfarðu skilgreiningu nýja króksins í `/hooks` í Codex og samþykktu hana þar. [Færni OFSAFAERNI](.agents/skills/ofsafaerni/SKILL.md)

Krókurinn skilar nöfnum og auðkennum samþykktra tillagna. Codex getur síðan hlaðið viðeigandi færni með `load`. Lesið er úr sömu færniskrá og `route` notar. Stillingin miðar við PowerShell á Windows og POSIX-skel annars staðar. Notaðu `hook-config --shell cmd` eða veldu aðra studda skel ef Codex-lotan notar hana. [Codex Hooks](https://learn.chatgpt.com/docs/hooks)

### Hermes

`collect --hermes` leitar í færnismöppu virka Hermes-prófílsins og möppum sem tilgreindar eru í `skills.create_dir` og `skills.external_dirs`. Þú getur einnig gefið möppu beint upp með `collect --root`. Lærðu verkferil með `/learn` í Hermes, safnaðu nýju færniskránni í OFSAFAERNI og samþykktu hana eftir yfirferð. Til að færa samþykkta færni í Hermes skaltu gefa færnismöppu viðkomandi prófíls upp sem `--out`.

```text
ofsafaerni export ID --out /slod/ad/hermes/skills
```

`export` varðveitir innihald og heiti samþykktra fylgiskráa og stöðvast ef sama færni er þegar til staðar á áfangastaðnum. Keyrsluréttindi Unix-skráa fylgja ekki með. Skráarsnið og vinnulag taka mið af skjölum Agent Skills og Hermes. [Agent Skills](https://agentskills.io/specification) · [Hermes Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)

## Prófanir

```text
python -m unittest discover -s tests -v
```

Prófin ná yfir færnival, íslenskar beygingar, óvissu og ótengdar beiðnir, söfnun, breytingar á samþykktum færniskrám, undirverk, lærdóm og flutning til Hermes. Prófunargögn geymslunnar eru tilbúin dæmi. Færniskrár, samþykki, endurgjöf og drög úr eigin verkefnum eru vistuð á tölvu notandans.

Keyrðu `python -m benchmarks.evaluate` til að fá sundurliðað mat á íslenskum og enskum beiðnum. [BÍN hjá Miðeind](https://github.com/mideind/BinPackage)
