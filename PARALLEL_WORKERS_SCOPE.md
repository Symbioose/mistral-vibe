# Scope — Subagents ouvriers parallèles (Emile)

Document de coordination pour bosser en parallèle sur le fork sans se marcher dessus.
Mon chantier : débloquer les **subagents capables d'écrire, en parallèle, sans conflit**.
Ton chantier (workflows déclaratifs) se construit **par-dessus** ce que je livre ici.

## Le gap qu'on fixe (vérifié dans le code)

- La concurrence existe déjà : plusieurs tool calls d'un même tour tournent en parallèle
  (`_run_tools_concurrently`, `vibe/core/agent_loop/_loop.py` ~l.1651).
- MAIS les subagents sont **read-only** : le seul builtin `explore` n'a que `grep` + `read_file`
  (`vibe/core/agents/models.py` l.142-148) et `prompts/task.md` dit "Subagents run read-only".
  → On peut paralléliser des éclaireurs, pas des ouvriers.
- Les events n'ont **pas d'identité d'agent** (`vibe/core/types.py`) : deux streams parallèles
  sont indistinguables.
- L'isolation git worktree existe (`vibe/core/worktree.py`) mais n'est pas branchée aux subagents.

## Ce que je livre (4 blocs)

1. **Identité d'agent sur les events** — champs `agent_id`, `parent_agent_id`, `agent_name`
   sur les events du loop. C'est LE contrat partagé (voir plus bas).
2. **Profils subagents ouvriers** — builtins write-capable (`coder`, `test-writer`, `doc-writer`)
   intégrés au système de permissions existant.
3. **Isolation par worktree** — arg `isolated` sur le task tool : chaque ouvrier reçoit son
   worktree git (réutilise `prepare_worktree_session`), branche nommée, merge-back par le parent,
   conflits **reportés** (pas d'auto-resolve).
4. **Sémantique d'approbation/annulation** — ouvriers auto-approuvés DANS leur worktree
   uniquement ; Ctrl-C propre ; un ouvrier qui crash n'emporte pas ses frères.

## Fichiers que JE modifie — ne touche pas à ceux-là

| Fichier | Quoi |
|---|---|
| `vibe/core/types.py` | identité d'agent sur les events (**contrat partagé — review à deux**) |
| `vibe/core/tools/builtins/task.py` | workers, arg `isolated`, worktrees, forward des events avec identité |
| `vibe/core/tools/builtins/prompts/task.md` | doc mise à jour (plus de "read-only" pour les workers) |
| `vibe/core/agents/models.py` | profils `coder`/`test-writer`/`doc-writer` + allowlist `TaskToolConfig` |
| `vibe/core/worktree.py` | petites extensions si besoin (naming/cleanup) — réutilisation surtout |
| `vibe/core/agent_loop/_loop.py` | tag minimal des events avec l'identité du loop |
| `vibe/core/config/` | clés `max_parallel_subagents`, `subagent_worktrees` |
| `tests/core/tools/builtins/test_task_*.py`, `tests/core/test_types.py` | tests (concurrence réelle, attribution, cancellation, isolation) |

## Fichiers à TOI (workflows) — je n'y touche pas

- Nouveau module `vibe/core/workflows/` (parsing TOML, stages, orchestration).
- `vibe/cli/commands.py` (commande `/workflow`) et widgets TUI associés.
- `.vibe/workflows/*.toml` d'exemple.

Zone grise : si tu dois toucher `task.py` ou `agents/models.py`, ping-moi d'abord.

## Le contrat d'events (draft v0 — À GELER ENSEMBLE avant d'implémenter)

Chaque event du loop porte :

```
agent_id: str          # unique par instance d'AgentLoop (uuid court)
parent_agent_id: str | None   # None pour le loop racine
agent_name: str        # "default", "coder", "test-writer", ...
```

Ce que tu peux supposer côté workflows :
- lancer N tasks `isolated=true` dans UN tour = N ouvriers réellement parallèles,
  chacun dans son worktree, events attribuables via `agent_id` ;
- le task tool retourne (par ouvrier) : branche créée, résumé, statut completed/failed ;
- le merge-back est déclenché par le parent, conflits remontés en erreur lisible.

Sémantiques précises (alignées sur le harness Claude Code, cf. mémo workflows) :
- **plafond de slots** : `max_parallel_subagents` (config, défaut ~4-6) — au-delà, les
  ouvriers font la queue ; la durée d'un fan-out = chemin critique, pas la moyenne ;
- **un ouvrier qui échoue → résultat `failed` + résumé d'erreur, jamais un crash du
  fan-out** : tu reçois toujours N résultats, tu filtres ;
- **sortie structurée** (schéma JSON validé par ouvrier, style `StructuredOutput`) :
  PHASE 2 — aujourd'hui tu reçois `response` en texte + statut. Si tu en as besoin
  absolument pour les workflows, ping-moi, on arbitre à midi.

La scène de visualisation (chatons) consommera exactement ces mêmes events plus tard —
ne dépends de rien d'autre que ce contrat.

## Ordre & synchro

1. **H+0** : mon agent produit `PARALLEL_WORKERS_SPEC.md` → on gèle le contrat d'events à deux (10 min).
2. Ensuite chacun implémente dans son couloir. Toi : mock le contrat en attendant
   (fixtures JSONL de faux events) pour ne jamais m'attendre.
3. Branches : `emile/parallel-workers` et `<toi>/workflows`. Rebase sur `main` du fork, pas de force-push (règle AGENTS.md du repo).
4. Vérif avant tout commit : `uv run pytest` + `uv run pyright` + `uv run ruff check --fix .` (conventions du repo).

## Hors scope (pour nous deux, aujourd'hui)

- Broadcast websocket + scène chatons (branchement plus tard sur le contrat d'events).
- Auto-résolution de conflits de merge (on reporte, on ne résout pas).
- Subagents récursifs (un ouvrier qui spawn des ouvriers) — interdit, comme aujourd'hui.
