"""Black-box regression coverage for the dependency-free ledger CLI."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/code-with-task-graphs/scripts/task_graph.py"
)


class TaskGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="task-graph-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.store = self.root / "ledger"
        self.run = "regression"
        self.ok("init", "Regression scenario", "--id", self.run, "--max-parallel", "2")

    def invoke(self, *arguments, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--store", str(self.store), *arguments],
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def ok(self, *arguments):
        result = self.invoke(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def rejected(self, *arguments, message):
        result = self.invoke(*arguments)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr + result.stdout)
        return result

    def state(self):
        return json.loads((self.store / f"{self.run}.json").read_text())

    def node(self, node_id, kind="research", dependencies=(), scope=None, checks=("inspect",)):
        arguments = [
            "node", self.run, node_id, "--title", node_id, "--kind", kind,
            "--objective", "Deliver the declared result", "--accept", "Result verified",
        ]
        for dependency in dependencies:
            arguments.extend(["--depends-on", dependency])
        for check in checks:
            arguments.extend(["--check", check])
        if scope:
            arguments.extend(["--scope", scope])
        self.ok(*arguments)

    def start(self, node_id):
        return json.loads(self.ok("start", self.run, node_id, "--owner", "test").stdout)["claim_token"]

    def pass_arguments(self, node_id, claim, results=("inspect: passed",), files=()):
        arguments = [
            "pass", self.run, node_id, "--claim", claim, "--worker-settled",
            "--summary", "Verified result", "--evidence", "Observed test evidence",
        ]
        for result in results:
            arguments.extend(["--check-result", result])
        for file in files:
            arguments.extend(["--file", file])
        return arguments

    def pass_node(self, node_id, **kwargs):
        self.ok(*self.pass_arguments(node_id, self.start(node_id), **kwargs))

    def fail_node(self, node_id, claim):
        self.ok(
            "fail", self.run, node_id, "--claim", claim, "--worker-settled",
            "--reason", "Observed failure", "--evidence", "Failing check",
        )

    def test_dependency_order_and_finalization_are_enforced(self):
        self.node("implement", kind="implementation", scope="src")
        self.node("validate", kind="validation", dependencies=("implement",))
        ready = json.loads(self.ok("ready", self.run).stdout)
        self.assertEqual([node["id"] for node in ready["ready"]], ["implement"])
        self.rejected("start", self.run, "validate", message="blocked by: implement")
        self.rejected("finalize", self.run, "--evidence", "Checks", message="incomplete nodes")
        self.pass_node("implement", files=("src/new.py",))
        self.pass_node("validate")
        self.ok("finalize", self.run, "--evidence", "Full check passed")
        before = self.state()
        self.assertEqual(before["schema_version"], 3)
        self.assertEqual(before["status"], "complete")
        self.rejected("retry", self.run, "implement", "--cause", "upstream", "--reason", "Changed", message="complete graph")
        self.assertEqual(self.state(), before)

    def test_equal_check_results_are_distinct_receipts(self):
        self.node("inspect", checks=("check-alpha", "check-beta"))
        claim = self.start("inspect")
        self.ok(*self.pass_arguments("inspect", claim, results=("passed", "passed")))
        self.assertEqual(self.state()["nodes"][0]["check_results"], ["passed", "passed"])

    def test_missing_check_result_does_not_close_attempt(self):
        self.node("inspect", checks=("check-alpha", "check-beta"))
        claim = self.start("inspect")
        self.rejected(*self.pass_arguments("inspect", claim), message="2 required, 1 supplied")
        self.assertEqual(self.state()["nodes"][0]["claim_token"], claim)

    def test_abandoned_graph_does_not_advertise_ready_work(self):
        self.node("inspect")
        self.ok("abandon", self.run, "--reason", "Superseded request")
        result = json.loads(self.ok("ready", self.run).stdout)
        self.assertEqual(result["ready"], [])
        self.rejected("start", self.run, "inspect", message="abandoned graph")

    def test_concurrent_claims_have_exactly_one_winner(self):
        self.node("inspect")
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: self.invoke("start", self.run, "inspect"), range(2)))
        self.assertEqual(sorted(result.returncode for result in claims), [0, 2])
        active = self.state()["nodes"][0]
        self.assertEqual(active["attempts"], 1)
        winner = next(result for result in claims if result.returncode == 0)
        self.assertEqual(active["claim_token"], json.loads(winner.stdout)["claim_token"])

    def test_retry_fences_stale_worker_and_preserves_independent_work(self):
        self.node("repair")
        self.node("independent")
        self.pass_node("independent")
        old_claim = self.start("repair")
        self.fail_node("repair", old_claim)
        self.ok("retry", self.run, "repair", "--cause", "check", "--reason", "Fix confirmed")
        new_claim = self.start("repair")
        self.assertNotEqual(old_claim, new_claim)
        self.rejected(*self.pass_arguments("repair", old_claim), message="stale worker")
        self.ok(*self.pass_arguments("repair", new_claim))
        nodes = {node["id"]: node for node in self.state()["nodes"]}
        self.assertEqual(nodes["independent"]["status"], "passed")
        self.assertEqual(nodes["independent"]["attempts"], 1)
        self.assertEqual(nodes["repair"]["attempts"], 2)
        self.assertEqual(nodes["repair"]["history"][0]["status"], "failed")

    def test_attempt_cap_stops_ordinary_retries(self):
        self.node("repair")
        for attempt in range(2):
            self.fail_node("repair", self.start("repair"))
            if attempt == 0:
                self.ok("retry", self.run, "repair", "--cause", "check", "--reason", "Narrow repair")
        self.rejected("retry", self.run, "repair", "--cause", "check", "--reason", "Again", message="attempt cap")

    def test_upstream_retry_invalidates_only_descendants(self):
        self.node("source")
        self.node("dependent", dependencies=("source",))
        self.node("independent")
        for node_id in ("source", "dependent", "independent"):
            self.pass_node(node_id)
        result = json.loads(self.ok("retry", self.run, "source", "--cause", "upstream", "--reason", "Input changed").stdout)
        self.assertEqual(result["reset"], ["dependent", "source"])
        self.assertEqual(result["preserved"], ["independent"])
        statuses = {node["id"]: node["status"] for node in self.state()["nodes"]}
        self.assertEqual(statuses, {"source": "pending", "dependent": "pending", "independent": "passed"})

    def test_overlapping_shared_writers_require_ordering(self):
        self.node("left", kind="implementation", scope="src")
        self.node("right", kind="implementation", scope="src/shared.py")
        self.node("validate", kind="validation", dependencies=("left", "right"))
        self.rejected("validate", self.run, message="Unordered writers left and right overlap")
        self.ok("node", self.run, "right", "--depends-on", "left")
        self.ok("validate", self.run)

    def test_cycle_and_missing_dependency_are_rejected(self):
        self.node("left", dependencies=("missing",))
        self.rejected("validate", self.run, message="missing dependency missing")
        self.node("missing", dependencies=("left",))
        self.rejected("validate", self.run, message="Dependency cycle")

    def test_receipts_cannot_escape_scope_or_checkout(self):
        self.node("implement", kind="implementation", scope="src")
        self.node("validate", kind="validation", dependencies=("implement",))
        claim = self.start("implement")
        for file, error in (("other.py", "outside the declared scope"), ("../outside.py", "parent-traversal")):
            with self.subTest(file=file):
                self.rejected(*self.pass_arguments("implement", claim, files=(file,)), message=error)
        self.assertEqual(self.state()["nodes"][0]["status"], "running")

    def test_repository_binding_and_single_active_run(self):
        self.rejected("init", "Second run", "--id", "second", message="already owns this checkout")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.invoke("status", self.run, cwd=elsewhere)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different checkout", result.stderr)

    def test_material_risk_requires_downstream_review(self):
        self.node("inspect")
        self.ok("node", self.run, "inspect", "--risk", "Public interface changes")
        self.rejected("validate", self.run, message="requires a review node")
        self.node("review", kind="review", dependencies=("inspect",))
        self.ok("validate", self.run)


if __name__ == "__main__":
    unittest.main()
