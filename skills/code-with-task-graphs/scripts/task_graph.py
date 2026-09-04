#!/usr/bin/env python3
"""Dependency-graph ledger for coding-agent workflows.

The script deliberately does not run tests, edit code, create worktrees, commit,
push, or deploy. Those actions remain visible to the host agent and its normal
permission controls. This runtime only validates and records orchestration state.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEFAULT_STORE = Path(".codex/task-graphs")
PASSING = {"passed"}
NODE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
WRITE_KINDS = {"implementation", "integration"}
RUN_TERMINAL = {"complete", "abandoned"}
ALIAS_SCAN_LIMIT = 100_000


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=True))


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def normalize_many(
    values: list[str] | None, *, split_commas: bool = False, unique: bool = True
) -> list[str] | None:
    if values is None:
        return None
    result: list[str] = []
    for value in values:
        for item in (value.split(",") if split_commas else [value]):
            item = item.strip()
            if item and (not unique or item not in result):
                result.append(item)
    return result


def has_text_items(values: list[str] | None) -> bool:
    return bool(values) and all(isinstance(item, str) and item.strip() for item in values)


def require_open_run(state: dict, operation: str) -> None:
    if state.get("status") in RUN_TERMINAL:
        fail(
            f"Cannot {operation} a {state.get('status')} graph run. Start a new run "
            "so the terminal ledger remains immutable."
        )


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def repo_relative_error(value: str, *, allow_glob: bool) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "path is blank"
    raw = value.replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return "absolute or drive-qualified paths are not allowed"
    if raw.startswith("~"):
        return "home-relative paths are not allowed"
    if "\x00" in raw:
        return "NUL bytes are not allowed"
    parts = raw.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return "empty, dot, and parent-traversal path segments are not allowed"
    if any(character in raw for character in "[]"):
        return "bracket globs are unsupported; use literal paths, *, ?, or **"
    if not allow_glob and any(character in raw for character in "*?"):
        return "changed-file paths must be concrete, not globs"
    return None


def nested_alias_error(directory: Path) -> str | None:
    stack = [directory]
    inspected = 0
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            return f"cannot verify alias safety under {current}: {exc}"
        for entry in entries:
            inspected += 1
            if inspected > ALIAS_SCAN_LIMIT:
                return (
                    f"scope exceeds the {ALIAS_SCAN_LIMIT}-entry alias-safety scan; "
                    "narrow the scope"
                )
            path = Path(entry.path)
            try:
                lexical = os.path.normcase(os.path.abspath(path))
                resolved = os.path.normcase(str(path.resolve()))
            except OSError as exc:
                return f"cannot verify alias safety for {path}: {exc}"
            if entry.is_symlink() or lexical != resolved:
                return f"nested symlink or junction detected at {path}"
            if entry.is_file(follow_symlinks=False):
                try:
                    if path.stat().st_nlink > 1:
                        return f"nested hard-linked file detected at {path}"
                except OSError as exc:
                    return f"cannot verify hard-link safety for {path}: {exc}"
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
    return None


def filesystem_path_error(
    value: str, repo_root: str, *, allow_glob: bool, deep_scan: bool = False
) -> str | None:
    root = Path(repo_root).resolve()
    raw = value.replace("\\", "/").strip().rstrip("/")
    literal_parts: list[str] = []
    for part in raw.split("/"):
        if allow_glob and any(character in part for character in "*?"):
            break
        literal_parts.append(part)
    candidate = root
    for part in literal_parts:
        candidate = candidate / part
        if not candidate.exists() and not candidate.is_symlink():
            continue
        lexical = os.path.normcase(os.path.abspath(candidate))
        resolved = os.path.normcase(str(candidate.resolve()))
        if lexical != resolved:
            return "symlink or junction path components are not allowed in graph scopes or receipts"
        try:
            candidate.resolve().relative_to(root)
        except ValueError:
            return "path resolves outside the recorded repository root"
    concrete = not allow_glob or not any(character in raw for character in "*?")
    if concrete and candidate.exists() and candidate.is_file():
        with contextlib.suppress(OSError):
            if candidate.stat().st_nlink > 1:
                return "hard-linked files are unsupported because aliases defeat scope isolation"
    if deep_scan and candidate.exists() and candidate.is_dir():
        return nested_alias_error(candidate)
    return None


def glob_regex(pattern: str) -> re.Pattern[str]:
    pieces = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    pieces.append("(?:.*/)?")
                    index += 1
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def glob_matches(path: str, pattern: str) -> bool:
    return bool(glob_regex(pattern).fullmatch(path))


def path_in_scope(path: str, scope: str) -> bool:
    path_n = normalize_path(path)
    scope_n = normalize_path(scope)
    if any(ch in scope_n for ch in "*?"):
        return glob_matches(path_n, scope_n)
    return path_n == scope_n or path_n.startswith(scope_n + "/")


def scopes_overlap(left: str, right: str) -> bool:
    left_exact = normalize_path(left)
    right_exact = normalize_path(right)
    return _scopes_overlap_normalized(left_exact, right_exact) or _scopes_overlap_normalized(
        left_exact.casefold(), right_exact.casefold()
    )


def _scopes_overlap_normalized(left_n: str, right_n: str) -> bool:
    left_wild = any(ch in left_n for ch in "*?")
    right_wild = any(ch in right_n for ch in "*?")
    if left_wild and glob_matches(right_n, left_n):
        return True
    if right_wild and glob_matches(left_n, right_n):
        return True
    left_prefix = re.split(r"[\*\?]", left_n, maxsplit=1)[0]
    right_prefix = re.split(r"[\*\?]", right_n, maxsplit=1)[0]
    if left_wild or right_wild:
        if not left_prefix or not right_prefix:
            return True
        return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
    if not left_n or not right_n:
        return True
    return (
        left_n == right_n
        or left_n.startswith(right_n + "/")
        or right_n.startswith(left_n + "/")
    )


def git_snapshot() -> dict:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = run("status", "--porcelain")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "status_known": status is not None,
        "dirty": None if status is None else bool(status),
        "status_porcelain": status,
    }


def canonical_root(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def current_repo_root() -> str:
    return canonical_root(Path.cwd())


def same_repo(recorded: str | None, current: str | None = None) -> bool:
    if not recorded:
        return False
    try:
        return canonical_root(recorded) == (current or current_repo_root())
    except OSError:
        return False


def require_repo_context(state: dict, store: Path) -> None:
    recorded = state.get("repo_root")
    if not recorded:
        fail("This ledger predates repository-root binding; create a new graph run.")
    current = current_repo_root()
    if not same_repo(recorded, current):
        fail(
            "This graph belongs to a different checkout. Run the ledger from its "
            f"recorded repository root: {recorded}"
        )
    recorded_store = state.get("store_path")
    if not recorded_store:
        fail("This ledger predates store-path binding; create a new graph run.")
    if canonical_root(recorded_store) != canonical_root(store):
        fail(
            "This graph belongs to a different state store. Use its recorded store: "
            f"{recorded_store}"
        )


def store_repo_scope(state: dict) -> str | None:
    try:
        relative = Path(state["store_path"]).resolve().relative_to(
            Path(state["repo_root"]).resolve()
        )
    except (KeyError, OSError, ValueError):
        return None
    value = relative.as_posix().rstrip("/")
    return value or None


def run_path(store: Path, run_id: str) -> Path:
    if not NODE_ID.fullmatch(run_id):
        fail(f"Invalid run id: {run_id!r}")
    return store / f"{run_id}.json"


def load_state(store: Path, run_id: str) -> tuple[Path, dict]:
    path = run_path(store, run_id)
    if not path.exists():
        fail(f"Unknown graph run: {run_id}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        require_repo_context(state, store)
        return path, state
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read graph state {path}: {exc}")


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


@contextlib.contextmanager
def state_lock(store: Path, run_id: str, timeout: float = 10.0):
    store.mkdir(parents=True, exist_ok=True)
    lock = store / f".{run_id}.lock"
    deadline = time.monotonic() + timeout
    handle = lock.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    acquired = False
    while not acquired:
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                handle.close()
                fail(f"Timed out waiting for graph lock: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def get_node(state: dict, node_id: str) -> dict:
    for node in state.get("nodes", []):
        if node.get("id") == node_id:
            return node
    fail(f"Unknown node: {node_id}")


def node_map(state: dict) -> dict[str, dict]:
    return {node["id"]: node for node in state.get("nodes", [])}


def add_event(state: dict, event_type: str, message: str, node: str | None = None) -> None:
    state.setdefault("events", []).append(
        {"at": utc_now(), "type": event_type, "node": node, "message": message}
    )


def dependency_descendants(state: dict, roots: set[str]) -> set[str]:
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for node in state.get("nodes", []):
            if node["id"] in selected:
                continue
            if any(dep in selected for dep in node.get("depends_on", [])):
                selected.add(node["id"])
                changed = True
    return selected


def transitive_dependencies(state: dict, node_id: str) -> set[str]:
    nodes = node_map(state)
    result: set[str] = set()
    stack = list(nodes.get(node_id, {}).get("depends_on", []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        if current in nodes:
            stack.extend(nodes[current].get("depends_on", []))
    return result


def graph_analysis(state: dict) -> dict:
    nodes = state.get("nodes", [])
    nodes_by_id = node_map(state)
    errors: list[str] = []
    warnings: list[str] = []

    if not nodes:
        errors.append("The graph has no nodes.")

    for node in nodes:
        node_id = node.get("id", "")
        for dependency in node.get("depends_on", []):
            if dependency == node_id:
                errors.append(f"Node {node_id} depends on itself.")
            elif dependency not in nodes_by_id:
                errors.append(f"Node {node_id} has missing dependency {dependency}.")
        if not isinstance(node.get("objective"), str) or not node.get("objective", "").strip():
            errors.append(f"Node {node_id} has no objective.")
        if not isinstance(node.get("title"), str) or not node.get("title", "").strip():
            errors.append(f"Node {node_id} has no title.")
        if not has_text_items(node.get("acceptance")):
            errors.append(f"Node {node_id} has no nonblank acceptance criteria.")
        if not has_text_items(node.get("checks")):
            errors.append(f"Node {node_id} has no nonblank declared checks.")
        if node.get("kind") in WRITE_KINDS and not node.get("scope"):
            errors.append(f"Write node {node_id} has no declared file scope.")
        for scope in node.get("scope", []):
            path_error = repo_relative_error(scope, allow_glob=True)
            if path_error:
                errors.append(f"Node {node_id} has invalid scope {scope!r}: {path_error}.")
                continue
            path_error = filesystem_path_error(
                scope,
                state.get("repo_root", current_repo_root()),
                allow_glob=True,
                deep_scan=node.get("kind") in WRITE_KINDS,
            )
            if path_error:
                errors.append(f"Node {node_id} has unsafe scope {scope!r}: {path_error}.")
        if node.get("kind") in WRITE_KINDS and node.get("isolation") == "read-only":
            errors.append(f"Write node {node_id} cannot use read-only isolation.")
        if node.get("max_attempts", 0) < 1:
            errors.append(f"Node {node_id} must allow at least one attempt.")

    write_nodes = [node for node in nodes if node.get("kind") in WRITE_KINDS]
    validation_nodes = [node for node in nodes if node.get("kind") == "validation"]
    integration_nodes = [node for node in nodes if node.get("kind") == "integration"]
    review_nodes = [node for node in nodes if node.get("kind") == "review"]
    if write_nodes and not validation_nodes:
        errors.append("A graph containing write nodes requires a validation node.")
    reserved_scope = store_repo_scope(state)
    if reserved_scope:
        for write_node in write_nodes:
            overlapping = [
                scope
                for scope in write_node.get("scope", [])
                if scopes_overlap(scope, reserved_scope)
            ]
            if overlapping:
                errors.append(
                    f"Write node {write_node['id']} scope overlaps the active ledger store "
                    f"{reserved_scope!r}: {', '.join(overlapping)}."
                )
    for write_node in write_nodes:
        if validation_nodes and not any(
            write_node["id"] in transitive_dependencies(state, validation["id"])
            for validation in validation_nodes
        ):
            errors.append(
                f"Write node {write_node['id']} does not feed any validation node."
            )

    risk_nodes = [
        node
        for node in nodes
        if node.get("kind") != "review"
        and isinstance(node.get("risk"), str)
        and node.get("risk", "").strip()
    ]
    review_required = len(write_nodes) >= 3 or bool(risk_nodes)
    if review_required:
        if not review_nodes:
            errors.append("A graph with material risk or at least three write nodes requires a review node.")
        else:
            review_inputs = [node["id"] for node in validation_nodes] + [
                node["id"] for node in risk_nodes
            ]
        if review_nodes and review_inputs and not any(
            all(
                required_id in transitive_dependencies(state, review["id"])
                for required_id in review_inputs
            )
            for review in review_nodes
        ):
            errors.append(
                "A required review node must run downstream of every validation and risk-bearing node."
            )

    if state.get("status") == "complete":
        incomplete = [
            node.get("id", "<unknown>")
            for node in nodes
            if node.get("status") != "passed"
        ]
        if incomplete:
            errors.append(
                "Finalized graph contains non-passed nodes: " + ", ".join(incomplete)
            )
        if not has_text_items(state.get("final_evidence")):
            errors.append("Finalized graph has no nonblank final evidence.")
    elif has_text_items(state.get("final_evidence")) or state.get("finished_at"):
        errors.append("Incomplete graph contains stale finalization metadata.")

    indegree = {node_id: 0 for node_id in nodes_by_id}
    children = {node_id: [] for node_id in nodes_by_id}
    for node in nodes:
        for dependency in node.get("depends_on", []):
            if dependency in nodes_by_id:
                indegree[node["id"]] += 1
                children[dependency].append(node["id"])

    layers: list[list[str]] = []
    current = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while current:
        layers.append(current)
        next_layer: list[str] = []
        for node_id in current:
            visited += 1
            for child in children[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_layer.append(child)
        current = sorted(next_layer)
    if visited != len(nodes_by_id):
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        errors.append("Dependency cycle detected involving: " + ", ".join(cyclic))

    for index, left in enumerate(write_nodes):
        left_deps = transitive_dependencies(state, left["id"])
        for right in write_nodes[index + 1 :]:
            right_deps = transitive_dependencies(state, right["id"])
            ordered = right["id"] in left_deps or left["id"] in right_deps
            if ordered:
                continue
            overlaps = [
                (a, b)
                for a in left["scope"]
                for b in right["scope"]
                if scopes_overlap(a, b)
            ]
            if not overlaps:
                continue
            detail = ", ".join(f"{a} <-> {b}" for a, b in overlaps[:3])
            if left.get("isolation") == "worktree" and right.get("isolation") == "worktree":
                fan_in = any(
                    left["id"] in transitive_dependencies(state, integration["id"])
                    and right["id"] in transitive_dependencies(state, integration["id"])
                    for integration in integration_nodes
                )
                if not fan_in:
                    errors.append(
                        f"Overlapping worktree nodes {left['id']} and {right['id']} ({detail}) "
                        "require a downstream integration node that depends on both branches."
                    )
                else:
                    warnings.append(
                        f"Overlapping worktree nodes {left['id']} and {right['id']} ({detail}); "
                        "verify they use distinct physical worktrees before dispatch."
                    )
            else:
                errors.append(
                    f"Unordered writers {left['id']} and {right['id']} overlap ({detail}); "
                    "add a dependency, narrow the scopes, or isolate both in worktrees."
                )

    ready = []
    blocked = []
    for node in nodes:
        if state.get("status") in RUN_TERMINAL or node.get("status") != "pending":
            continue
        dependencies = [nodes_by_id.get(dep) for dep in node.get("depends_on", [])]
        if dependencies and any(dep is None for dep in dependencies):
            continue
        if all(dep.get("status") in PASSING for dep in dependencies):
            ready.append(node["id"])
        else:
            blocked.append(
                {
                    "id": node["id"],
                    "waiting_on": [
                        dep["id"] for dep in dependencies if dep.get("status") not in PASSING
                    ],
                }
            )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "layers": layers,
        "ready": sorted(ready),
        "blocked": blocked,
    }


def peer_writer_conflicts(store: Path, run_id: str, target: dict) -> list[str]:
    if target.get("kind") not in WRITE_KINDS or target.get("isolation") == "worktree":
        return []
    conflicts: list[str] = []
    current = current_repo_root()
    for path in store.glob("*.json"):
        if path.stem == run_id:
            continue
        try:
            other = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"Cannot inspect peer graph ledger {path}: {exc}")
        running_writers = [
            node
            for node in other.get("nodes", [])
            if node.get("status") == "running"
            and node.get("kind") in WRITE_KINDS
            and node.get("isolation") != "worktree"
        ]
        if not running_writers:
            continue
        if not other.get("repo_root"):
            fail(
                f"Peer graph {other.get('id', path.stem)} has running writers but no "
                "repository binding; stop or migrate it before starting more work."
            )
        if not same_repo(other.get("repo_root"), current):
            continue
        for peer in running_writers:
            overlaps = [
                (left, right)
                for left in target.get("scope", [])
                for right in peer.get("scope", [])
                if scopes_overlap(left, right)
            ]
            if overlaps:
                detail = ", ".join(f"{left} <-> {right}" for left, right in overlaps[:3])
                conflicts.append(
                    f"{other.get('id', path.stem)}/{peer.get('id')} ({detail})"
                )
    return conflicts


def cmd_init(args) -> None:
    store = args.store
    run_id = args.id or f"graph-{uuid.uuid4().hex[:8]}"
    path = run_path(store, run_id)
    if not args.goal.strip():
        fail("The graph goal must be nonblank.")
    repo_root = current_repo_root()
    store_path = canonical_root(store)
    if store_path == repo_root:
        fail("--store cannot be the repository root; use a dedicated subdirectory or external path.")
    with state_lock(store, "__store__"):
        for peer_path in store.glob("*.json"):
            try:
                peer = json.loads(peer_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                fail(f"Cannot inspect existing graph ledger {peer_path}: {exc}")
            if peer.get("status") not in RUN_TERMINAL and same_repo(peer.get("repo_root"), repo_root):
                fail(
                    f"Incomplete graph {peer.get('id', peer_path.stem)} already owns this "
                    "checkout. Resume or finish it, or use a separate worktree."
                )
        with state_lock(store, run_id):
            if path.exists():
                fail(f"Graph run already exists: {run_id}")
            state = {
                "schema_version": 3,
                "id": run_id,
                "goal": args.goal.strip(),
                "repo_root": repo_root,
                "store_path": store_path,
                "status": "planning",
                "graph_version": 1,
                "max_parallel": args.max_parallel,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "git_at_start": git_snapshot(),
                "nodes": [],
                "events": [],
                "final_evidence": [],
            }
            add_event(state, "init", args.goal.strip())
            save_state(path, state)
    emit({"run": run_id, "path": str(path), "goal": state["goal"], "repo_root": repo_root})


def cmd_node(args) -> None:
    if not NODE_ID.fullmatch(args.node):
        fail(f"Invalid node id: {args.node!r}")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "edit")
        existing = next(
            (item for item in state.get("nodes", []) if item["id"] == args.node), None
        )
        if existing and existing.get("status") in {"running", "passed"}:
            fail(
                f"Cannot edit {existing['status']} node {args.node}; invalidate it first if its contract changed."
            )
        if existing is None:
            if not args.title or not args.title.strip():
                fail("--title is required when creating a node")
            existing = {
                "id": args.node,
                "title": args.title.strip(),
                "kind": args.kind or "implementation",
                "objective": args.objective if args.objective is not None else args.title.strip(),
                "depends_on": normalize_many(args.depends_on, split_commas=True) or [],
                "inputs": normalize_many(args.inputs) or [],
                "outputs": normalize_many(args.outputs) or [],
                "scope": normalize_many(args.scope) or [],
                "acceptance": normalize_many(args.acceptance) or [],
                "checks": normalize_many(args.checks) or [],
                "risk": args.risk,
                "rollback": args.rollback,
                "isolation": args.isolation
                or ("read-only" if (args.kind or "implementation") in {"research", "review", "decision"} else "shared"),
                "status": "pending",
                "owner": None,
                "claim_token": None,
                "worker_settled": None,
                "attempts": 0,
                "max_attempts": 2 if args.max_attempts is None else args.max_attempts,
                "evidence": [],
                "check_results": [],
                "files": [],
                "result_summary": None,
                "error": None,
                "history": [],
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            state.setdefault("nodes", []).append(existing)
            state["graph_version"] = state.get("graph_version", 1) + 1
            action = "created"
        else:
            updates = {
                "title": args.title,
                "kind": args.kind,
                "objective": args.objective,
                "depends_on": normalize_many(args.depends_on, split_commas=True),
                "inputs": normalize_many(args.inputs),
                "outputs": normalize_many(args.outputs),
                "scope": normalize_many(args.scope),
                "acceptance": normalize_many(args.acceptance),
                "checks": normalize_many(args.checks),
                "risk": args.risk,
                "rollback": args.rollback,
                "isolation": args.isolation,
                "max_attempts": args.max_attempts,
            }
            for key, value in updates.items():
                if value is not None:
                    existing[key] = value
            existing["updated_at"] = utc_now()
            state["graph_version"] = state.get("graph_version", 1) + 1
            action = "updated"
        add_event(state, f"node_{action}", existing["title"], args.node)
        save_state(path, state)
    emit({"run": args.run, "node": existing, "action": action})


def cmd_validate(args) -> None:
    _, state = load_state(args.store, args.run)
    analysis = graph_analysis(state)
    emit({"run": args.run, **analysis})
    if not analysis["valid"]:
        raise SystemExit(1)


def cmd_ready(args) -> None:
    _, state = load_state(args.store, args.run)
    analysis = graph_analysis(state)
    if not analysis["valid"]:
        emit({"run": args.run, **analysis})
        raise SystemExit(1)
    nodes = node_map(state)
    ready = [nodes[node_id] for node_id in analysis["ready"]]
    emit(
        {
            "run": args.run,
            "max_parallel": state.get("max_parallel", 1),
            "ready": ready,
            "blocked": analysis["blocked"],
            "warnings": analysis["warnings"],
        }
    )


def cmd_start(args) -> None:
    with state_lock(args.store, "__store__"):
        with state_lock(args.store, args.run):
            path, state = load_state(args.store, args.run)
            require_open_run(state, "start work in")
            analysis = graph_analysis(state)
            if not analysis["valid"]:
                fail("Graph is invalid; run validate and repair it before starting work.")
            node = get_node(state, args.node)
            if node.get("status") != "pending":
                fail(f"Node {args.node} is {node.get('status')}, not pending.")
            if args.node not in analysis["ready"]:
                waiting = next(
                    (item["waiting_on"] for item in analysis["blocked"] if item["id"] == args.node),
                    [],
                )
                fail(f"Node {args.node} is blocked by: {', '.join(waiting)}")
            running_count = sum(
                1 for item in state.get("nodes", []) if item.get("status") == "running"
            )
            if running_count >= state.get("max_parallel", 1):
                fail(
                    f"Concurrency limit reached ({state.get('max_parallel', 1)}); wait for a running node."
                )
            if node.get("attempts", 0) >= node.get("max_attempts", 2):
                fail(f"Node {args.node} reached its attempt cap; replan instead of looping.")
            conflicts = peer_writer_conflicts(args.store, args.run, node)
            if conflicts:
                fail("Overlapping shared writer already running: " + "; ".join(conflicts))
            claim_token = uuid.uuid4().hex
            node["status"] = "running"
            node["owner"] = args.owner
            node["claim_token"] = claim_token
            node["worker_settled"] = False
            node["attempts"] = node.get("attempts", 0) + 1
            node["started_at"] = utc_now()
            node["updated_at"] = utc_now()
            state["status"] = "running"
            add_event(state, "node_started", args.owner or "unassigned", args.node)
            save_state(path, state)
    emit(
        {
            "run": args.run,
            "node": args.node,
            "status": "running",
            "owner": args.owner,
            "claim_token": claim_token,
        }
    )


def archive_attempt(node: dict) -> None:
    if node.get("result_summary") or node.get("error") or node.get("evidence"):
        node.setdefault("history", []).append(
            {
                "attempts": node.get("attempts", 0),
                "status": node.get("status"),
                "summary": node.get("result_summary"),
                "error": node.get("error"),
                "evidence": node.get("evidence", []),
                "check_results": node.get("check_results", []),
                "worker_settled": node.get("worker_settled"),
                "files": node.get("files", []),
                "archived_at": utc_now(),
            }
        )


def cmd_pass(args) -> None:
    evidence = normalize_many(args.evidence) or []
    # Receipts correspond to declared checks in order; equal outcomes are valid.
    check_results = normalize_many(args.check_results, unique=False) or []
    if not args.summary.strip():
        fail("--summary is required to pass a node")
    if not has_text_items(evidence):
        fail("At least one nonblank --evidence item is required to pass a node")
    if not args.worker_settled:
        fail("Confirm the worker returned or was stopped with --worker-settled before passing.")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "pass a node in")
        node = get_node(state, args.node)
        if node.get("status") != "running":
            fail(f"Node {args.node} is {node.get('status')}, not running.")
        if not args.claim or node.get("claim_token") != args.claim:
            fail("Claim token does not match the active attempt; reject stale worker output.")
        declared_checks = node.get("checks", [])
        if len(check_results) != len(declared_checks):
            fail(
                f"Record exactly one nonblank --check-result for each declared check "
                f"({len(declared_checks)} required, {len(check_results)} supplied)."
            )
        files = normalize_many(args.files) or []
        if node.get("kind") in WRITE_KINDS and not files:
            fail("A passed write node must record at least one --file.")
        if node.get("kind") not in WRITE_KINDS and files:
            fail("Only implementation and integration nodes may record changed files.")
        invalid_files = []
        for file in files:
            path_error = repo_relative_error(file, allow_glob=False)
            if not path_error:
                path_error = filesystem_path_error(
                    file, state["repo_root"], allow_glob=False
                )
            if path_error:
                invalid_files.append(f"{file!r}: {path_error}")
        if invalid_files:
            fail("Invalid changed-file paths: " + "; ".join(invalid_files))
        reserved_scope = store_repo_scope(state)
        reserved_files = [
            file
            for file in files
            if reserved_scope and path_in_scope(file, reserved_scope)
        ]
        if reserved_files:
            fail("Changed files may not touch the active ledger store: " + ", ".join(reserved_files))
        out_of_scope = [
            file
            for file in files
            if node.get("scope") and not any(path_in_scope(file, scope) for scope in node["scope"])
        ]
        if out_of_scope:
            fail(
                "Files outside the declared scope: "
                + ", ".join(out_of_scope)
                + ". Fail the node, revise its scope, validate, and retry."
            )
        node.update(
            {
                "status": "passed",
                "claim_token": None,
                "worker_settled": True,
                "worker_settled_at": utc_now(),
                "result_summary": args.summary,
                "evidence": evidence,
                "check_results": check_results,
                "files": files,
                "error": None,
                "finished_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        add_event(state, "node_passed", args.summary, args.node)
        save_state(path, state)
    emit(
        {
            "run": args.run,
            "node": args.node,
            "status": "passed",
            "evidence": evidence,
            "check_results": check_results,
        }
    )


def cmd_fail(args) -> None:
    evidence = normalize_many(args.evidence) or []
    if not args.reason.strip():
        fail("--reason must be nonblank")
    if not args.worker_settled:
        fail("Interrupt or await the worker, then confirm --worker-settled before failing it.")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "fail a node in")
        node = get_node(state, args.node)
        if node.get("status") != "running":
            fail(f"Node {args.node} is {node.get('status')}, not running.")
        if not args.claim or node.get("claim_token") != args.claim:
            fail("Claim token does not match the active attempt; reject stale worker output.")
        node.update(
            {
                "status": "failed",
                "claim_token": None,
                "worker_settled": True,
                "worker_settled_at": utc_now(),
                "error": args.reason,
                "evidence": evidence,
                "finished_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        add_event(state, "node_failed", args.reason, args.node)
        save_state(path, state)
    emit({"run": args.run, "node": args.node, "status": "failed", "reason": args.reason})


def cmd_retry(args) -> None:
    if not args.reason.strip():
        fail("--reason must be nonblank")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "retry work in")
        target = get_node(state, args.node)
        if target.get("status") == "running":
            fail("Do not invalidate a running node; stop or fail its worker first.")
        if args.cause in {"transient", "check"} and target.get("status") != "failed":
            fail(f"A {args.cause} retry requires a failed target node.")
        if args.cause in {"transient", "check"} and target.get("attempts", 0) >= target.get("max_attempts", 2):
            fail(f"Node {args.node} reached its attempt cap; revise the graph or request human input.")
        selected = {args.node}
        if args.downstream or args.cause in {"upstream", "graph"}:
            selected = dependency_descendants(state, selected)
        running_selected = sorted(
            node["id"]
            for node in state.get("nodes", [])
            if node["id"] in selected and node.get("status") == "running"
        )
        if running_selected:
            fail(
                "Stop or fail running affected nodes before invalidation: "
                + ", ".join(running_selected)
            )
        for node in state.get("nodes", []):
            if node["id"] not in selected:
                continue
            archive_attempt(node)
            node["status"] = "pending"
            node["owner"] = None
            node["claim_token"] = None
            node["worker_settled"] = None
            node["worker_settled_at"] = None
            node["result_summary"] = None
            node["error"] = None
            node["evidence"] = []
            node["check_results"] = []
            node["files"] = []
            node["started_at"] = None
            node["finished_at"] = None
            node["updated_at"] = utc_now()
            if node["id"] != args.node or args.cause in {"upstream", "graph"}:
                node["attempts"] = 0
        if args.cause == "graph":
            state["graph_version"] = state.get("graph_version", 1) + 1
        add_event(
            state,
            "retry_planned",
            f"cause={args.cause}; nodes={','.join(sorted(selected))}; reason={args.reason}",
            args.node,
        )
        save_state(path, state)
    emit(
        {
            "run": args.run,
            "cause": args.cause,
            "reset": sorted(selected),
            "preserved": sorted(node["id"] for node in state["nodes"] if node["id"] not in selected),
        }
    )


def ascii_tree(state: dict) -> str:
    analysis = graph_analysis(state)
    symbols = {
        "pending": "[ ]",
        "running": "[>]",
        "passed": "[x]",
        "failed": "[!]",
        "cancelled": "[-]",
    }
    lines = [
        f"Graph {state['id']} | {state.get('status')} | v{state.get('graph_version', 1)}",
        f"Goal: {state.get('goal')}",
    ]
    nodes = node_map(state)
    for layer_number, layer in enumerate(analysis["layers"]):
        lines.append(f"Layer {layer_number}:")
        for node_id in layer:
            node = nodes[node_id]
            dependencies = ",".join(node.get("depends_on", [])) or "-"
            lines.append(
                f"  {symbols.get(node.get('status'), '[?]')} {node_id}: {node.get('title')} "
                f"(deps: {dependencies}; attempts: {node.get('attempts', 0)}/{node.get('max_attempts', 2)})"
            )
    if analysis["errors"]:
        lines.append("Errors: " + " | ".join(analysis["errors"]))
    if analysis["ready"]:
        lines.append("Ready: " + ", ".join(analysis["ready"]))
    return "\n".join(lines)


def cmd_tree(args) -> None:
    _, state = load_state(args.store, args.run)
    print(ascii_tree(state))


def cmd_status(args) -> None:
    _, state = load_state(args.store, args.run)
    emit({"run": state, "analysis": graph_analysis(state)})


def cmd_resume(args) -> None:
    candidates: list[tuple[float, str]] = []
    if not args.store.exists():
        fail("No task-graph store exists.")
    current = current_repo_root()
    for path in args.store.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("status") not in RUN_TERMINAL and same_repo(state.get("repo_root"), current):
            with contextlib.suppress(OSError):
                candidates.append((path.stat().st_mtime, state.get("id", path.stem)))
    if not candidates:
        fail("No incomplete graph run found for this checkout.")
    for _, run_id in sorted(candidates, reverse=True):
        with state_lock(args.store, run_id):
            path, state = load_state(args.store, run_id)
            if state.get("status") in RUN_TERMINAL:
                continue
            running = [
                node["id"]
                for node in state.get("nodes", [])
                if node.get("status") == "running"
            ]
            emit(
                {
                    "run": state["id"],
                    "path": str(path),
                    "goal": state.get("goal"),
                    "repo_root": state.get("repo_root"),
                    "status": state.get("status"),
                    "git_at_start": state.get("git_at_start"),
                    "git_now": git_snapshot(),
                    "running_requires_inspection": running,
                    "analysis": graph_analysis(state),
                }
            )
            return
    fail("No incomplete graph run found for this checkout.")


def cmd_abandon(args) -> None:
    if not args.reason.strip():
        fail("--reason must be nonblank")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "abandon")
        running = [
            node["id"]
            for node in state.get("nodes", [])
            if node.get("status") == "running"
        ]
        if running:
            fail(
                "Cannot abandon while nodes are running. Fail or otherwise settle them first: "
                + ", ".join(running)
            )
        state["status"] = "abandoned"
        state["abandoned_at"] = utc_now()
        state["abandon_reason"] = args.reason.strip()
        add_event(state, "abandoned", args.reason.strip())
        save_state(path, state)
    emit(
        {
            "run": args.run,
            "status": "abandoned",
            "reason": args.reason.strip(),
            "ledger": str(path),
        }
    )


def cmd_finalize(args) -> None:
    evidence = normalize_many(args.evidence) or []
    if not has_text_items(evidence):
        fail("At least one nonblank --evidence item is required for final integration verification.")
    with state_lock(args.store, args.run):
        path, state = load_state(args.store, args.run)
        require_open_run(state, "finalize")
        analysis = graph_analysis(state)
        if not analysis["valid"]:
            fail("Cannot finalize an invalid graph.")
        incomplete = [
            f"{node['id']}={node.get('status')}"
            for node in state.get("nodes", [])
            if node.get("status") != "passed"
        ]
        if incomplete:
            fail("Cannot finalize; incomplete nodes: " + ", ".join(incomplete))
        state["status"] = "complete"
        state["final_evidence"] = evidence
        state["finished_at"] = utc_now()
        state["git_at_finish"] = git_snapshot()
        add_event(state, "finalized", "; ".join(evidence))
        save_state(path, state)
    print(ascii_tree(state))
    print("Final evidence:")
    for item in evidence:
        print(f"  - {item}")
    print(f"Ledger: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="Graph-state directory (default: .codex/task-graphs)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("init", help="Create a graph run")
    command.add_argument("goal")
    command.add_argument("--id")
    command.add_argument("--max-parallel", type=int, default=4)
    command.set_defaults(handler=cmd_init)

    command = commands.add_parser("node", help="Create or update a pending node")
    command.add_argument("run")
    command.add_argument("node")
    command.add_argument("--title")
    command.add_argument(
        "--kind",
        choices=["research", "decision", "implementation", "integration", "validation", "review"],
    )
    command.add_argument("--objective")
    command.add_argument("--depends-on", action="append")
    command.add_argument("--input", dest="inputs", action="append")
    command.add_argument("--output", dest="outputs", action="append")
    command.add_argument("--scope", action="append")
    command.add_argument("--accept", dest="acceptance", action="append")
    command.add_argument("--check", dest="checks", action="append")
    command.add_argument("--risk")
    command.add_argument("--rollback")
    command.add_argument("--isolation", choices=["shared", "worktree", "read-only"])
    command.add_argument("--max-attempts", type=int)
    command.set_defaults(handler=cmd_node)

    for name, handler, help_text in (
        ("validate", cmd_validate, "Validate dependencies, cycles, contracts, and scopes"),
        ("ready", cmd_ready, "List nodes whose dependencies passed"),
        ("tree", cmd_tree, "Print a compact graph"),
        ("status", cmd_status, "Print complete graph state and analysis"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run")
        command.set_defaults(handler=handler)

    command = commands.add_parser("start", help="Atomically claim a ready node")
    command.add_argument("run")
    command.add_argument("node")
    command.add_argument("--owner")
    command.set_defaults(handler=cmd_start)

    command = commands.add_parser("pass", help="Mark a running node passed with evidence")
    command.add_argument("run")
    command.add_argument("node")
    command.add_argument("--claim", required=True, help="Claim token returned by start")
    command.add_argument(
        "--worker-settled",
        action="store_true",
        help="Assert that the worker returned or was stopped and cannot make more writes",
    )
    command.add_argument("--summary", required=True)
    command.add_argument("--evidence", action="append")
    command.add_argument(
        "--check-result",
        dest="check_results",
        action="append",
        help="Result receipt; repeat exactly once per declared check",
    )
    command.add_argument("--file", dest="files", action="append")
    command.set_defaults(handler=cmd_pass)

    command = commands.add_parser("fail", help="Mark a running node failed")
    command.add_argument("run")
    command.add_argument("node")
    command.add_argument("--claim", required=True, help="Claim token returned by start")
    command.add_argument(
        "--worker-settled",
        action="store_true",
        help="Assert that the worker returned or was stopped and cannot make more writes",
    )
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", action="append")
    command.set_defaults(handler=cmd_fail)

    command = commands.add_parser("retry", help="Reset a failed/invalidated node and optionally descendants")
    command.add_argument("run")
    command.add_argument("node")
    command.add_argument("--cause", required=True, choices=["transient", "check", "upstream", "graph"])
    command.add_argument("--reason", required=True)
    command.add_argument("--downstream", action="store_true")
    command.set_defaults(handler=cmd_retry)

    command = commands.add_parser("resume", help="Find the newest incomplete graph")
    command.set_defaults(handler=cmd_resume)

    command = commands.add_parser("abandon", help="Seal an obsolete non-running graph")
    command.add_argument("run")
    command.add_argument("--reason", required=True)
    command.set_defaults(handler=cmd_abandon)

    command = commands.add_parser("finalize", help="Complete a fully passed graph with integration evidence")
    command.add_argument("run")
    command.add_argument("--evidence", action="append")
    command.set_defaults(handler=cmd_finalize)

    return parser


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parser = build_parser()
    args = parser.parse_args()
    args.store = args.store.resolve()
    if getattr(args, "max_parallel", 1) < 1:
        fail("--max-parallel must be at least 1")
    args.handler(args)


if __name__ == "__main__":
    main()
