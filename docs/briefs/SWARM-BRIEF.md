# Brief — Implémenter `swarm()` : fan-out massif (centaines d'agents) dans meow_meow_meow

> Brief auto-suffisant pour un agent frais ; aucune dépendance à une conversation passée.
> Ancres vérifiées sur `feat/workflows` au 2026-07-18, tip `96da5a2`.
> Si le terrain contredit ce brief, LE TERRAIN GAGNE : vérifie, adapte, note la
> divergence dans la PR.

## Prérequis & décisions réservées (à lire avant tout)
- Aucun input humain bloquant. Les tests runtime utilisent un spawner factice (zéro
  API). Le benchmark live (optionnel, étape 6) dépense la clé Mistral configurée de
  l'utilisateur (keyring/env, résolue par `VibeConfig.load()`) — signale le coût estimé
  en tête de PR, ne le lance pas si tu n'y arrives pas, ce n'est pas bloquant.
- Décisions à NE PAS prendre seul : changer le nom du tool (`meow_meow_meow`), toucher
  au format du journal (rétro-compatibilité requise), fusionner dans `feat/workflows`
  ou `main`. Tu livres une PR de `feat/swarm` vers `feat/workflows`, non mergée.

## 0. But & contexte
- **LE BUT.** Ajouter au runtime meow_meow_meow une primitive `swarm()` qui lance des
  CENTAINES d'agents homogènes en parallèle de façon fiable (backpressure, retries
  rate-limit, journal) avec une UI chat qui reste lisible à 300 agents. Succès mesurable :
  un swarm de 200 items s'exécute avec concurrence ≥ 16, l'UI chat reste sous ~15 lignes
  par phase, un re-run rejoue 100 % depuis le journal (0 appel API), tous les checks du
  repo sont verts.
- **État réel au 2026-07-18** (mesuré, pas estimé) : le runtime existe et parallélise —
  benchmark live commité : 12 agents concurrents, 36 fichiers audités en 28,2 s /
  115 307 tokens vs 44,1 s / 221 791 pour un agent seul, recall 100 % des deux côtés,
  resume journal = 0 token (voir `docs/meowmeowmeow-benchmark.md`). Il n'existe AUCUN
  symbole `swarm` dans `vibe/` (vérifié par grep — négatif).
- **Sources de vérité (lire dans cet ordre)** :
  1. `AGENTS.md` — conventions du repo (uv, ruff, pyright strict, pas d'imports relatifs,
     pydantic v2, match/case, pas de `# type: ignore` inline).
  2. `vibe/core/meowmeowmeow/runtime.py` — le runtime : sémaphore, caps, journal, events.
  3. `vibe/core/meowmeowmeow/script.py` — validation AST, primitives réservées, caps script.
  4. `vibe/core/tools/builtins/meow_meow_meow.py` — le tool + `_AgentLoopSpawner`.
  5. `vibe/cli/textual_ui/widgets/meow_meow_meow.py` — widget chat (phases, rows, pruning).
  6. `vibe/core/tools/builtins/prompts/meow_meow_meow.md` — le prompt vu par le LLM.
  7. `tests/meowmeowmeow/test_runtime.py` — le pattern FakeSpawner à réutiliser.
  8. `docs/meowmeowmeow-benchmark.md` + `scripts/benchmark_meowmeowmeow.py`.
- **Le principe directeur** : swarm n'est PAS un nouveau système — c'est une primitive de
  plus dans le runtime existant, qui réutilise sémaphore, journal, spawner, events, UI.
  Toute duplication de cette machinerie est un défaut de design.
- **Décisions déjà prises (ne pas re-débattre)** : Python async scripté par le modèle ;
  prose dans l'argument `prompts` (caps durs 200 lignes / 250 chars par string) ;
  noms de primitives réservés ; journal par (prompt, schema, agent_name, model) ;
  sub-agents = profils SUBAGENT uniquement ; UI minimaliste orange Mistral.
- **Périmètre** : `vibe/core/meowmeowmeow/*`, `vibe/core/tools/builtins/meow_meow_meow.py`
  (+ son prompt md), `vibe/cli/textual_ui/widgets/meow_meow_meow*.py`, tests et docs
  associés. NON affectés : le tool `task`, l'orchestrateur du collaborateur (travail
  séparé, état inconnu — expose la même seam `SubagentSpawner`, ne t'y couple pas),
  l'ACP, teleport (ses "workflows" = GitHub Actions, AUCUN rapport), `.github/workflows/`.

## Carte des ancres vérifiées (ne pas inventer)
| ancre | fichier | fait clé |
|---|---|---|
| `MeowMeowMeowRuntime` | `vibe/core/meowmeowmeow/runtime.py` | `_agent()` acquiert `self._semaphore` ; compteur `_agent_total` cap 1000 (`DEFAULT_MAX_AGENTS`) |
| `MAX_FANOUT_ITEMS = 4096` | `runtime.py` (haut de fichier) | cap par appel parallel/pipeline |
| `default_max_concurrency()` | `runtime.py` | `min(16, cpu-2)` — le goulot réel d'un swarm est l'API, pas le CPU |
| `SubagentSpawner` (Protocol) | `runtime.py` | seam unique de spawn ; l'impl réelle est `_AgentLoopSpawner` dans le tool |
| `RESERVED_PRIMITIVES` | `vibe/core/meowmeowmeow/script.py` | y ajouter `swarm` sinon un script peut shadow la primitive |
| `_collect_missing_awaits` | `script.py` | y ajouter `swarm` dans `_AWAITABLE_PRIMITIVES` |
| événements | `vibe/core/meowmeowmeow/events.py` | union discriminée par `kind` ; le widget consomme des dicts `model_dump(mode="json")` via `ToolStreamEvent.data` |
| `MeowMeowMeowPhaseGroup` | `widgets/meow_meow_meow.py` | pruning existant : `_KEEP_FINISHED_ROWS_PER_PHASE = 6`, compteurs started/finished |
| `fast_model` | runtime + config du tool | alias modèle rapide exposé aux scripts (peut être `None`) — les shards swarm doivent le prendre par défaut |
| `swarm` | — | N'EXISTE PAS (négatif vérifié) → à créer |
| journal | `vibe/core/meowmeowmeow/journal.py` | multiset keyed par hash ; seuls les succès sont enregistrés |
| état de branche | re-dérive : `git fetch origin && git log origin/feat/workflows -1 --oneline` | ne fais pas confiance au sha du brief |

## Étapes ordonnées (chaque étape = commit ; acceptance dans le titre)

### 1. Primitive `swarm()` dans le runtime · les tests FakeSpawner passent
`await swarm(items, brief, *, schema=None, label=None, agent_name=None, model=None, concurrency=None)` :
- `brief` : str (préfixe commun) ou callable `(item, index) -> str` ; prompt final =
  brief + rendu de l'item. `label` idem (défaut : `swarm:{index}`).
- Sémantique = `parallel` (barrier, erreurs → `None`), mais : cap d'items dédié
  (`MAX_SWARM_ITEMS = 4096` partagé), `model` par défaut = `fast_model` s'il est set,
  et un sémaphore de swarm séparé si `concurrency` est passé (sinon celui du runtime).
- Émet des événements agrégés (étape 2) au lieu d'un row UI par agent au-delà d'un seuil.
- Réutilise `_agent()` en interne (journal, schema-retry, events par agent restent vrais).
Acceptance : tests unitaires (pattern `FakeSpawner` de `tests/meowmeowmeow/test_runtime.py`) :
ordre des résultats préservé, erreurs isolées → `None`, concurrence ≤ cap mesurée via
`max_active`, replay journal intégral au 2e run, `swarm` réservé/await-vérifié par le
validateur de script.

### 2. Backpressure & résilience rate-limit dans le spawner · test de retry vert
Dans `_AgentLoopSpawner` (tool) : détection des échecs transitoires (429 / 5xx /
timeouts — inspecte ce que `loop.act()` lève réellement AVANT de coder : re-dérive en
lisant `vibe/core/llm/backend`) et retry borné avec backoff exponentiel + jitter
(module `random` autorisé ICI — l'interdiction de nondéterminisme ne concerne que le
namespace des scripts). Cap de retries dans la config du tool (`spawn_retries: int = 2`).
Acceptance : test avec un spawner qui échoue N fois puis réussit ; le swarm converge
sans perdre d'items.

### 3. Événements agrégés · l'UI chat reste minimale à 300 agents
Nouvel événement `SwarmProgressEvent {kind:"swarm_progress", swarm_id, label, total,
running, done, failed, cached, sample_labels: list[str]}` (échantillon des agents
actifs, ≤ 4). Le runtime l'émet à chaque transition (start/finish d'un membre), throttlé
(≥ 250 ms entre deux émissions pour le même swarm — `time.monotonic` autorisé runtime).
Côté widget : au-delà de ~8 agents dans une phase issus d'un même swarm, remplacer les
rows individuels par UNE ligne de swarm : barre de progression textuelle
(`▰▰▰▰▱▱▱▱ 96/200 · 12 running · 3 ✕ · sample: scan:41, scan:87`), orange en cours,
✓ à la fin. L'inspecteur (ctrl+w) garde l'arbre complet — c'est là qu'on fouille.
Acceptance : test widget existant style `_Harness` : 200 agents simulés → la phase rend
1 ligne de swarm + compteurs corrects ; l'inspecteur liste bien les 200.

### 4. Surface script & prompt · le LLM sait quand swarmer
- `prompts/meow_meow_meow.md` : documenter `swarm()` (table API + un exemple), règle :
  "> ~10 items homogènes → `swarm()`, pas une liste de lambdas dans `parallel()`" ;
  rappeler `fast_model` par défaut pour les shards.
- `docs/meowmeowmeow.md` : section swarm.
Acceptance : `test_tool_name_and_description`-style : la description générée contient
`swarm`.

### 5. Statique + suites complètes vertes (gate obligatoire)
`uv run ruff check --fix . && uv run ruff format .` ; `uv run pyright` (les ~16 erreurs
POSIX préexistantes sous Windows — signal/pty/fcntl/tzset — ne sont PAS à toi ; aucune
nouvelle erreur dans tes fichiers) ; `uv run pytest tests/meowmeowmeow
tests/cli/textual_ui/test_meow_meow_meow_widget.py
tests/cli/textual_ui/test_meow_meow_meow_inspector.py
tests/cli/textual_ui/test_meow_meow_meow_event_dispatch.py
tests/cli/textual_ui/test_lazy_startup_imports.py -q` → 0 échec.
`test_lazy_startup_imports` est un GATE : n'importe jamais `vibe.core.agent_loop` au
niveau module dans du code atteint par l'import de la TUI (le spawner l'importe
lazily dans une méthode — préserve ça).

### 6. (Optionnel, non bloquant) étendre le benchmark
`scripts/benchmark_meowmeowmeow.py` : un mode `--swarm` qui utilise `swarm()` au lieu du
parallel de lambdas, à ≥ 96 fichiers. Rapporte wall/tokens/recall dans la PR. Dépense la
clé API de l'utilisateur : estime le coût d'abord (~120-300 k tokens) et note-le.

## Les pièges que seul ce contexte connaissait
- **`__pycache__` périmé** : après des renames massifs, du bytecode obsolète a fait
  planter l'app avec un mélange ancien-code/nouvelles-lignes. Si tu vois un traceback
  incohérent : purge `Get-ChildItem -Path vibe -Recurse -Directory -Filter __pycache__ |
  Remove-Item -Recurse -Force`.
- **PowerShell 5.1 + git commit** : les guillemets doubles DANS une here-string `@'...'@`
  passée à `git commit -m` cassent l'argument (incident réel : message coupé à
  `"audit vibe/core"`). Pas de `"` dans les messages de commit, ou `git commit -F fichier`.
- **Textual : sélection programmatique** : `tree.select_node()` déclenche
  `NodeHighlighted` de façon ASYNCHRONE — un flag posé autour de l'appel ne suffit pas.
  Pattern qui marche (déjà dans `meow_meow_meow_inspector.py`) : mémoriser
  `_expected_selection` et le consommer dans le handler.
- **`StatusMessage._state` est privé** : utilise la propriété publique `indicator_state`
  (ajoutée pour ça) — la règle du repo interdit les accès privés inter-classes hors tests.
- **Ruff mordra** : caps `too-many-statements` (50) / `too-many-branches` / valeurs
  magiques → extrais des helpers d'entrée de jeu ; `jsonschema.validators` doit
  s'importer `from jsonschema.validators import validator_for` (pyright).
- **pytest** : mode asyncio STRICT (`@pytest.mark.asyncio` requis), timeout 10 s par
  test (garde les sleeps ≤ 0.01), xdist actif (pas d'état global partagé entre tests —
  attention à `MeowMeowMeowCallMessage.instances`, une liste de classe).
- **Warnings CRLF au commit** : normaux sur ce repo Windows, ignore-les.
- **`ToolStreamEvent.data`** : le widget reçoit des DICTS (pas les objets pydantic) —
  les tests widget passent des dicts littéraux ; garde cette symétrie pour swarm_progress.

## Garde-fous (non négociables)
- Travaille dans un WORKTREE isolé : `git fetch origin && git worktree add
  ../mistral-vibe-swarm -b feat/swarm origin/feat/workflows`, `uv sync --dev` dedans.
  Le checkout principal est utilisé par d'autres sessions — n'y commite jamais.
- Un writer par worktree ; PR de `feat/swarm` → `feat/workflows`, OUVERTE, PAS mergée.
- Rétro-compatibilité : tous les tests meow existants passent SANS modification de leur
  sémantique (tu peux en ajouter, pas en affaiblir).
- Style AGENTS.md : typage moderne, pas de commentaires narratifs, pydantic
  `model_validate`, logging `logger` avec `%s`.
- Fin de tâche : `git log --branches --not --remotes --oneline` → aucun commit orphelin.

## Hors-scope (NE PAS construire ici)
- L'orchestrateur multi-agents du collaborateur (branche séparée, état inconnu) — la
  seam `SubagentSpawner` suffit.
- Durabilité cross-process / Mistral Studio Workflows (produit cloud distinct — aucun
  rapport malgré le nom).
- Budgets de tokens par swarm (`budget`-style) — noté comme extension future dans la PR.
- Toute modification du tool `task` ou de l'ACP.

## Inconnus non vérifiés
- Le comportement exact de l'API Mistral sous 429 (forme de l'exception levée par le
  backend) — re-dérive en lisant `vibe/core/llm/backend/` avant l'étape 2.
- La limite de concurrence côté compte Mistral de l'utilisateur (les runs réels ont
  montré 12 agents concurrents OK ; 100+ simultanés n'a jamais été testé live).
- L'état de la branche du collaborateur (orchestrateur) — jamais vue dans cette session.

## Acceptance globale & ordre
Scénario de bout en bout : un script contenant
`hits = await swarm(items_200, prompts["scan"], schema=SCAN_SCHEMA)` s'exécute avec
concurrence ≥ 16, l'UI chat montre 1 ligne de swarm vivante (barre + compteurs + sample)
sous sa phase, l'inspecteur liste les 200 agents, un re-run avec `resume_from_run_id`
rejoue les 200 en < 1 s / 0 appel API, et `pytest` + `ruff` + `pyright` sont au niveau
décrit à l'étape 5. Ordre : 1 → 5 strict, 6 optionnel. Livrable : numéro de PR + liste
des tests ajoutés + paragraphe risques résiduels + ce que tu as sciemment laissé de côté.
