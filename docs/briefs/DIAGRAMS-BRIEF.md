# Brief — Livrer les diagrammes Mermaid complets des deux features (HTML autonome)

> Brief auto-suffisant pour un agent frais ; aucune dépendance à une conversation passée.
> Ancres vérifiées sur `origin/main` au 2026-07-18 (re-dérive le tip :
> `git fetch origin && git log origin/main -1 --oneline`).
> Si le terrain contredit ce brief, LE TERRAIN GAGNE : vérifie, adapte, note la
> divergence dans la PR.

## Prérequis & décisions réservées
- Aucun input humain, aucune clé API : tâche 100 % lecture de code + rédaction.
- Décisions à NE PAS prendre seul : renommer les features (les noms produits sont
  **MeowMeowMeow** et **parallel workers / orchestrateur**), merger la PR, modifier du
  code source (tu ne touches QUE `docs/diagrams/`).

## 0. But & contexte
- **LE BUT.** Deux pages HTML autonomes dans `docs/diagrams/` — `meowmeowmeow.html` et
  `parallel-workers.html` — contenant des diagrammes Mermaid complets et EXACTS
  (architecture, séquence, états, pipeline de validation) des deux features du fork.
  Succès mesurable : chaque page s'ouvre dans un navigateur par double-clic, tous les
  diagrammes se rendent sans erreur de syntaxe Mermaid, et chaque nœud/flèche correspond
  à du code réel que tu as lu (pas de composant inventé).
- **Contexte produit.** Ce fork de mistral-vibe ajoute :
  1. **MeowMeowMeow** — un tool builtin (`meow_meow_meow`) où le LLM écrit un script
     Python async orchestrant des sub-agents en parallèle (primitives `agent()`,
     `parallel()`, `pipeline()`, `phase()`, `log()`), avec validation statique stricte,
     journal de replay (resume), sorties JSON validées par schéma, et une TUI live
     (arbre de phases, inspecteur ctrl+w, chaton braille).
  2. **Parallel workers (orchestrateur)** — le tool `task` étendu : profil `worker`
     write-capable, chaque worker isolé dans un worktree git, sémaphore `max_parallel`,
     merge-back des branches par le parent, conflits reportés, UI « fat cat + kittens ».
- **Sources de vérité (lire dans cet ordre, AVANT de dessiner)** :
  1. `PARALLEL_WORKERS_SCOPE.md` (racine du repo) — l'intention de l'orchestrateur.
  2. `docs/meowmeowmeow.md` + `vibe/core/tools/builtins/prompts/meow_meow_meow.md` —
     l'intention et le contrat de MeowMeowMeow.
  3. `vibe/core/meowmeowmeow/` — `script.py` (validation), `runtime.py` (exécution),
     `journal.py`, `events.py`, `models.py`, `structured.py`.
  4. `vibe/core/tools/builtins/meow_meow_meow.py` — le tool + `_AgentLoopSpawner`.
  5. `vibe/cli/textual_ui/widgets/meow_meow_meow.py` + `meow_meow_meow_inspector.py`
     + `vibe/cli/textual_ui/handlers/event_handler.py` (dispatch UI).
  6. `vibe/core/tools/builtins/task.py` — workers, `isolated`, worktrees, merge-back.
  7. `vibe/core/agents/models.py` — profils (`EXPLORE`, `WORKER`, `AgentIsolation`).
  8. `vibe/cli/textual_ui/widgets/cats.py` — fat cat / kittens.
- **Le principe directeur** : un diagramme n'affirme QUE ce que le code fait. Chaque
  boîte porte le nom réel du composant (classe/fichier), chaque arête un événement ou
  appel réel. Si tu n'as pas lu le code correspondant, la boîte n'existe pas.
- **Périmètre** : création de `docs/diagrams/` uniquement. NON affectés : tout le code
  source, les tests, les prompts, `.github/` (ses « workflows » = CI GitHub, rien à
  voir), teleport (idem).

## Carte des ancres vérifiées (ne pas inventer)
| ancre | où | fait clé |
|---|---|---|
| validation script | `vibe/core/meowmeowmeow/script.py` | rejets : meta non littéral, imports, dunder, noms réservés (`RESERVED_PRIMITIVES`), await manquant, `await agent` dans un `for`, aucun await top-level, >200 lignes, string >250 chars |
| runtime | `vibe/core/meowmeowmeow/runtime.py` | sémaphore `min(16, cpu-2)`, cap 1000 agents, `parallel`=barrier/erreurs→None, `pipeline`=sans barrier, journal replay, run 0-agent = échec |
| journal | `vibe/core/meowmeowmeow/journal.py` | clé = hash(prompt, schema, agent_name, model), multiset, seuls les succès enregistrés |
| événements | `vibe/core/meowmeowmeow/events.py` | union discriminée `kind` : phases_planned (émis par le tool), phase_started, agent_started/progress/finished, log, finished |
| spawner | `builtins/meow_meow_meow.py` `_AgentLoopSpawner` | AgentLoop par agent (SUBAGENT only, refus write/isolation), sidecar `<script>.prompts.json`, `fast_model` |
| UI meow | `widgets/meow_meow_meow.py` | phases ○/◆/✓, rows avec chrono, activité, kitten label « N kittens hunting » ; inspecteur = ModalScreen ctrl+w (tree + prompt/activité/output, follow) |
| task workers | `builtins/task.py` | `max_parallel` (sémaphore d'état), profil write → worktree obligatoire, branche par worker, merge-back policy, suffixes `[branch x]`/`[no changes]` |
| profil WORKER | `agents/models.py` (~l.159 — re-confirme avant de citer) | SUBAGENT, `isolation=WORKTREE`, tools grep/read_file/edit/write_file/bash/todo |
| cats | `widgets/cats.py` | `KITTEN_ART`/`FAT_CAT_ART` braille ; fat cat « orchestrator · N kittens dispatched » épinglé au-dessus du premier tool call |
| `docs/diagrams/` | — | N'EXISTE PAS → à créer |

## Étapes ordonnées

### 1. Lire, puis lister les diagrammes (plan court en tête de PR)
Lis les sources dans l'ordre. Produis pour chaque feature la liste fermée des
diagrammes (voir menu ci-dessous), avec pour chacun les fichiers sources qui le
justifient. C'est ton contrat de contenu.

**Menu attendu — `meowmeowmeow.html`** (tous) :
- `flowchart` architecture : LLM → tool → validateur → runtime (primitives, sémaphore,
  journal) → `_AgentLoopSpawner` → N AgentLoop ; flux d'événements → `ToolStreamEvent.data`
  → EventHandler → widget chat + inspecteur.
- `sequenceDiagram` d'un run nominal : invoke → validation → phases_planned →
  fan-out parallèle (montrer 3 agents concurrents) → schema retry sur un agent →
  journal.record → result. Ajouter le chemin resume (replay 0 appel).
- `stateDiagram-v2` du cycle de vie d'un run : validé/rejeté → running → completed /
  failed (script error, 0-agent) / cancelled → resumable via journal.
- `flowchart` du pipeline de validation : chaque règle de rejet dans l'ordre réel de
  `parse_meow_meow_meow_script` (syntaxe → meta → caps lignes → violations collectées →
  await top-level), sorties = liste COMPLÈTE d'erreurs d'un coup.

**Menu attendu — `parallel-workers.html`** (tous) :
- `flowchart` architecture : loop parent → tool task (sémaphore `max_parallel`) →
  profil worker → worktree par worker (branche) → merge-back → conflits reportés ;
  events avec identité d'agent → UI fat cat/kittens.
- `sequenceDiagram` : 3 workers dispatchés dans UN tour, travail concurrent dans
  3 worktrees, commits, merges séquentiels par le parent, cas conflit (reporté, pas
  résolu), tests finaux.
- `stateDiagram-v2` du cycle de vie d'un worker : dispatché → worktree créé → running →
  committed → merged / conflict-reported / failed (un échec n'emporte pas les frères).

Acceptance : le plan liste ≥ 7 diagrammes au total, chacun avec ses fichiers sources.

### 2. Construire les deux pages HTML · rendu par double-clic
- Un fichier HTML autonome par feature dans `docs/diagrams/`, léger et lisible :
  titre, sommaire, un `<h2>` + un paragraphe d'intro (2-3 phrases max) par diagramme,
  puis le bloc `<pre class="mermaid">`.
- Mermaid via CDN (`<script type="module"> import mermaid from
  "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({startOnLoad: true, theme: "neutral"}); </script>`) — ces pages
  s'ouvrent en local, pas de CSP.
- Style sobre : fond clair, accent orange Mistral `#FF8205` pour les titres, police
  système. Pas de framework.
- Labels des nœuds = noms réels (`MeowMeowMeowRuntime`, `_AgentLoopSpawner`,
  `journal.jsonl`, `max_parallel`…) ; texte d'accompagnement en français.
Acceptance : ouvrir chaque fichier dans un navigateur → tous les diagrammes rendus,
zéro « Syntax error in graph ».

### 3. Vérification syntaxique mécanique avant livraison
Les erreurs Mermaid sont silencieuses jusqu'au rendu. Vérifie chaque diagramme :
- si `npx` est disponible : `npx -y @mermaid-js/mermaid-cli@11 -i <fichier.mmd> -o /tmp/x.svg`
  sur chaque bloc (extrais les blocs en `.mmd` temporaires) ;
- sinon : ouvre réellement chaque page (`start docs/diagrams/meowmeowmeow.html` sous
  Windows) et inspecte visuellement, puis note « vérifié au rendu navigateur » dans la PR.
Pièges syntaxe fréquents : pas de `()` ni `{}` non échappés dans les labels de nœuds
(utilise `["…"]`), pas d'accents dans les IDs de nœuds (accents OK dans les labels),
`participant` déclarés avant usage dans les séquences.

### 4. PR
Branche `docs/diagrams` depuis `origin/main`, PR ouverte NON mergée, description =
le plan de l'étape 1 + comment tu as vérifié le rendu.

## Les pièges que seul ce contexte connaissait
- **Deux « workflows » homonymes** : `.github/workflows/` (CI) et le produit Mistral
  Studio « Workflows » n'ont RIEN à voir avec MeowMeowMeow. Ne les mentionne pas.
- **Le checkout principal est PARTAGÉ et change de branche sans prévenir** (3 incidents
  dans la session d'origine : commits atterris sur la mauvaise branche, module
  introuvable par bytecode croisé). Travaille exclusivement dans TON worktree.
- **`__pycache__` périmé** après changements de branche → tracebacks incohérents ;
  purge si tu vois du code « fantôme ».
- **PowerShell 5.1** : les guillemets doubles dans un message de `git commit -m @'...'@`
  cassent l'argument (incident réel). Pas de `"` dans les messages, ou `git commit -F`.
- **Les caps du validateur meow s'appliquent aux SCRIPTS meow, pas à ton HTML** — mais
  si tu cites un script meow d'exemple dans un diagramme, respecte sa vraie grammaire
  (voir `scripts/demo_audit.meow`, script canné réel et validé).
- Le fichier `PARALLEL_WORKERS_SPEC.md` est mentionné dans le scope doc mais n'a
  JAMAIS été vu dans cette session — ne le cite pas sans l'avoir trouvé.

## Garde-fous (non négociables)
- Worktree isolé : `git fetch origin && git worktree add ../mistral-vibe-diagrams -b
  docs/diagrams origin/main` ; un writer par worktree ; jamais de commit depuis le
  checkout principal ; fin de tâche : `git log --branches --not --remotes --oneline`
  → rien d'orphelin.
- Tu ne modifies AUCUN fichier hors `docs/diagrams/`.
- Zéro invention : si un flux n'est pas clair dans le code, note-le « à confirmer »
  dans la PR plutôt que de dessiner une hypothèse.

## Hors-scope
- Diagrammes du benchmark, de la TUI générale de vibe, de l'ACP, de teleport.
- Toute retouche de code, de tests ou de prompts.
- Publication en Artifact/web — livrable = fichiers HTML dans le repo (note : si un
  jour ces pages deviennent des Artifacts, Mermaid y est rendu nativement via
  ` ```mermaid `/`<pre class="mermaid">` SANS CDN — mentionne-le en commentaire HTML).

## Inconnus non vérifiés
- Disponibilité de `npx` sur la machine (d'où le fallback navigateur à l'étape 3).
- L'état exact du merge-back « policy » dans la config du task tool (lis
  `TaskToolConfig` avant de dessiner cette arête).

## Acceptance globale & ordre
Un relecteur ouvre les deux pages par double-clic : chaque feature y est compréhensible
de bout en bout (architecture → séquence → états → validation), tous les diagrammes se
rendent, chaque nom de nœud existe dans le code. Ordre : 1 → 4 strict. Livrable : numéro
de PR + le plan des diagrammes + la méthode de vérification du rendu + la liste de ce
que tu as marqué « à confirmer ».
