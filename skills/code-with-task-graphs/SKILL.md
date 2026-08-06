---
name: code-with-task-graphs
description: Plan and execute complex coding work as a validated dependency graph of bounded tasks, quality gates, parallel agents, fan-in integration, and localized retries. Use when the user asks for graph engineering, a task DAG, dependency-aware coding, parallel coding agents, a resumable multi-step feature, selective retries, or an alternative to one long agent or Ralph-style loop. Do not use for charts, data visualization, graph databases, code knowledge graphs, or a trivial edit that gains nothing from decomposition.
---

# Code with Task Graphs

Treat the project as a graph of verifiable work, not as one long conversation. Use a directed acyclic graph for the coarse workflow and bounded implement-check-repair loops inside individual nodes.

This skill includes `scripts/task_graph.py`, a dependency-free state ledger. Within a repository-bound run, it validates the graph, calculates ready work, atomically claims nodes with stale-worker fencing, rejects unordered shared-checkout writers whose declared scopes overlap, scans write scopes for filesystem aliases, flags declared worktree overlaps for human verification, records evidence, preserves unrelated completed nodes during retry, and blocks finalization until every valid node has passed and full-run evidence is recorded. The ledger checks that receipts are present and nonblank; it cannot independently prove that a reported command ran, that a receipt is truthful, or that a declared worktree is physically isolated. It does not edit code, run commands, spawn agents, create worktrees, cache model calls, commit, push, or deploy.

## Choose the smallest useful workflow

- Use a normal single-agent workflow for a clear one-file edit with no meaningful design decision or cross-cutting risk.
- Use a small graph for work spanning roughly two to four files, one design decision, or separate implementation and verification stages.
- Use a full graph for five or more files, multiple design decisions, cross-layer work, migrations, public APIs, authentication, concurrency, or other high-risk changes.
- Honor an explicit graph request for a small task, but keep it to one implementation node and one validation node.
- Do not create more worker nodes than genuinely independent work scopes.

State the chosen size and why in one sentence before creating the graph.

## Inspect before planning

1. Read the relevant repository guidance and current Git status.
2. Inspect entry points, interfaces, tests, build commands, and existing architectural boundaries.
3. Preserve unrelated user changes.
4. Check for an existing incomplete ledger and resume it. Keep one active graph per checkout; use a separate real worktree for unrelated concurrent work.
5. Detect available subagents and worktree isolation. Fall back to serial execution when they are unavailable.
6. If the repository already uses a persistent task system such as Beads, use that existing source of truth rather than creating a second ledger. Do not install or initialize an external task system without the user's permission.

## Define a node contract

Give every node:

- a stable lowercase ID;
- one objective and one independently checkable deliverable;
- explicit blocking dependencies;
- declared inputs and outputs;
- a narrow read or write scope;
- acceptance criteria and deterministic checks;
- risk and rollback notes when material;
- an isolation mode: `read-only`, `shared`, or `worktree`;
- a maximum of two execution attempts by default.

Use node kinds deliberately:

- `research` or `decision` for uncertainty that must be resolved before coding;
- `implementation` for a bounded code change;
- `integration` for serialized fan-in of parallel branches;
- `validation` for deterministic end-to-end checks;
- `review` for fresh-context inspection after objective checks pass.

Make dependencies describe real artifact flow. Do not connect tasks merely to force an attractive shape. Keep blocking edges separate from optional context or related-work notes.

## Create and validate the graph

Resolve `scripts/task_graph.py` relative to this `SKILL.md`. Run it from the repository root and use forward-slash repository-relative scopes. Use an available Python 3 interpreter for every command below. If Python 3 is unavailable, follow the same node contract manually and execute serially; disclose that atomic claims, automated graph validation, and ledger guarantees are unavailable.

Initialize a run:

```text
python <skill-dir>/scripts/task_graph.py init "<goal>" --max-parallel 4
```

Create each node. Repeat list options once per item:

```text
python <skill-dir>/scripts/task_graph.py node <run> <id> --title "<short title>" --kind implementation --objective "<bounded objective>" --depends-on <upstream-id> --input "<required artifact or fact>" --output "<deliverable>" --scope "<owned path>" --accept "<observable completion condition>" --check "<exact verification command>" --isolation shared
```

Omit `--depends-on` for root nodes. Use forward-slash repo-relative paths for scopes. Do not declare scopes through symlinks, junctions, or hard-link aliases; use the canonical repository path instead. Never include the active `.codex/task-graphs` ledger store in a write scope. For a genuinely whole-repository operation, use a dedicated store outside the repository and pass the same `--store <absolute-path>` before every subcommand.

Validate before implementation:

```text
python <skill-dir>/scripts/task_graph.py validate <run>
python <skill-dir>/scripts/task_graph.py tree <run>
```

Fix every validation error. In particular:

- reject missing dependencies and cycles;
- add an explicit dependency between shared-checkout writers whose scopes overlap;
- narrow scopes or use actual worktrees for parallel writers;
- add a serialized integration node downstream of both branches when isolated worktrees overlap;
- add acceptance criteria before dispatch;
- include a validation node after implementation sinks;
- add a fresh-context review node for large or risky work. The runtime enforces this when any node declares risk or the graph has at least three write nodes; use judgment for other large changes.

## Execute topological waves

List the ready set:

```text
python <skill-dir>/scripts/task_graph.py ready <run>
```

Run only nodes returned as ready, up to the graph's concurrency limit.

Before dispatch, atomically claim each node:

```text
python <skill-dir>/scripts/task_graph.py start <run> <id> --owner <agent-or-main>
```

Save the unique `claim_token` returned by `start`. It fences delayed receipts from an older attempt, but it cannot stop an old worker from writing. Await the worker or interrupt it and confirm it is quiescent before closing or retrying its node.

For each delegated node, give the worker only the context it needs:

- the overall goal;
- the complete node contract;
- upstream outputs and evidence;
- relevant repository guidance;
- the exact owned paths;
- the checks it must run;
- an instruction not to edit outside its scope;
- the required return receipt: summary, files changed, commands run, results, and remaining risks.

Use subagents for bounded independent work. Keep the main agent responsible for the graph, contracts, dependency changes, integration, and final truthfulness.

Parallelize read-only exploration freely when useful. Parallelize writers only when their scopes are disjoint or each has a real isolated worktree. Never assume separate agent context implies separate filesystems.

After a worker returns:

1. Inspect the actual diff and repository state.
2. Run the node's deterministic checks through the normal tool and permission flow.
3. Compare the result with every acceptance criterion.
4. Record the node as passed only with concrete evidence:

```text
python <skill-dir>/scripts/task_graph.py pass <run> <id> --claim <claim-token> --worker-settled --summary "<what changed>" --check-result "<declared check: result>" --evidence "<artifact or reviewed fact>" --file "<repo-relative changed file>"
```

Repeat `--check-result` exactly once per declared check and `--file` once per changed file. Omit `--file` for research, decision, validation, and review nodes because those nodes may not record code changes.

Do not treat an agent's claim of completion as evidence by itself.

## Handle failures locally

Record a failed attempt:

```text
python <skill-dir>/scripts/task_graph.py fail <run> <id> --claim <claim-token> --worker-settled --reason "<specific failure>" --evidence "<failing command or observation>"
```

Classify the failure before retrying:

- `transient`: the environment or tool failed without invalidating the plan;
- `check`: the implementation failed a deterministic check;
- `upstream`: an upstream artifact changed and descendants are stale;
- `graph`: the node contract or dependency structure was wrong.

Reset only the affected work:

```text
python <skill-dir>/scripts/task_graph.py retry <run> <id> --cause check --reason "<why another attempt is justified>"
```

The `upstream` and `graph` causes automatically invalidate every descendant. Add `--downstream` to a `transient` or `check` retry only when its effects already reached dependent work. Preserve successful upstream and independent branches.

Use at most two attempts for ordinary transient or check failures. If the same failure repeats, stop retrying, inspect the assumption, revise the graph, or ask for the missing decision. Never turn the entire graph into an unbounded loop.

If a node edits outside its contract, fail it, reset it as a graph problem, revise the pending node's scope, validate again, and then restart. The ledger rejects an out-of-scope pass.

## Resume safely

Find the newest incomplete run:

```text
python <skill-dir>/scripts/task_graph.py resume
```

On resume:

1. Compare the recorded Git baseline with the current repository.
2. Inspect any node still marked running; never assume its worker survived. Before failing or reclaiming it, await or interrupt the old worker and confirm it cannot make more writes. Preserve its current claim token only if that exact attempt is still authoritative; otherwise fail it with that token and `--worker-settled`, retry it, and use the new token so delayed receipts cannot close the replacement attempt.
3. Verify that passed-node files and artifacts still exist and remain valid.
4. Invalidate changed nodes and their descendants when external edits made their evidence stale.
5. Continue from the newly calculated ready set.

The ledger checkpoints node status and receipts within a run. A finalized ledger is immutable; create a new run for later changes. Do not describe this as cached model calls or restored code snapshots.

If an obsolete graph has no running nodes and should not be resumed, seal it before starting a replacement:

```text
python <skill-dir>/scripts/task_graph.py abandon <run> --reason "<why this plan is obsolete>"
```

Never abandon a graph merely to hide failed work; retain the ledger and report the reason.

## Integrate and finish

After all implementation branches pass:

1. Run a serialized integration node when branches require merging or reconciliation; otherwise use the validation node as their fan-in gate.
2. Run the declared full test, lint, type, build, migration, or runtime checks.
3. Use a fresh-context reviewer for large or risky changes. Ask it to inspect the diff and evidence, not to repeat implementation.
4. Convert real review failures into targeted repair nodes or retries, then rerun affected validation.
5. Confirm every required node is passed.
6. Finalize only with full-run evidence:

```text
python <skill-dir>/scripts/task_graph.py finalize <run> --evidence "<full verification command and result>" --evidence "<independent review result when required>"
```

Report the graph ID, completed nodes, retries, files changed, verification evidence, and remaining risks. Include the local ledger path so the work can be resumed or audited.

## Respect authority boundaries

- Do not commit unless the user explicitly requests a commit.
- If a commit is authorized, stage only the files owned by the relevant node and preserve unrelated changes.
- Do not push, deploy, publish, merge, or open a pull request without explicit authorization.
- Do not install orchestration tools or enable experimental features merely to satisfy this workflow.
- Prefer deterministic repository checks over additional model opinions.
