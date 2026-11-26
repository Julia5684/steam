"""Focused regressions for concurrent handoffs, recovery and plan revisions."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import flow


class ConcurrentFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="steam-flow-test-")
        self.workspace = Path(self.directory.name).resolve()
        self.cfg = flow.config()
        self.config_patch = patch.object(flow, "config", lambda: copy.deepcopy(self.cfg))
        self.config_patch.start()
        self.sync_patch = patch("sync_supervisor.check_synced")
        self.sync_patch.start()

    def tearDown(self):
        self.sync_patch.stop()
        self.config_patch.stop()
        self.directory.cleanup()

    def run_for(self, rows):
        steps = flow.validate({"steps":rows}, self.cfg, self.workspace)
        return flow.new_run(self.cfg, self.workspace, steps)

    def row(self, sid, route="project_analysis", needs=None):
        return {"id":sid, "route":route, "brief":"test", "needs":needs or []}

    def workers(self, run_worker):
        return patch.dict(sys.modules, {"workers":types.SimpleNamespace(
            run_worker=run_worker, read_progress=lambda directory: {})})

    def test_independent_steps_overlap_without_duplicate_dispatch(self):
        run = self.run_for([self.row("a"), self.row("b")])
        first = threading.Event()
        both = threading.Event()
        release = threading.Event()
        calls, results, errors = [], [], []

        def worker(*args, **kwargs):
            calls.append(args[6].name)
            first.set()
            if len(calls) == 2:
                both.set()
            self.assertTrue(release.wait(5))
            return {"status":"success", "summary":"done"}

        def invoke(sid):
            try:
                results.append(flow.concurrent_advance(run, one_step=True, step_id=sid))
            except BaseException as error:
                errors.append(error)

        with self.workers(worker):
            a = threading.Thread(target=invoke, args=("a",))
            b = threading.Thread(target=invoke, args=("b",))
            a.start()
            self.assertTrue(first.wait(3))
            try:
                with self.assertRaises(flow.Busy):
                    flow.concurrent_advance(run, one_step=True, step_id="a")
                b.start()
                self.assertTrue(both.wait(3))
                status = flow.query_result(run)
                self.assertEqual(status["status"], "running")
                self.assertEqual(set(status["active_steps"]), {"a", "b"})
                self.assertFalse(status["progress"]["all_settled"])
            finally:
                release.set()
                a.join(5)
                if b.ident:
                    b.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(calls), ["a", "b"])
            self.assertEqual(flow.query_result(run)["status"], "success")

    def test_native_handoff_does_not_block_independent_external_step(self):
        run = self.run_for([self.row("native", "documentation"), self.row("external")])
        with self.workers(lambda *args, **kwargs: {"status":"success", "summary":"external done"}):
            handoff = flow.concurrent_advance(run, one_step=True, step_id="native")
            self.assertIn("claim", handoff["next"])
            out = flow.concurrent_advance(run, one_step=True, step_id="external")
            self.assertEqual(out["status"], "waiting_claude")
            self.assertFalse(out["progress"]["all_settled"])
            reply = self.workspace / "reply.json"
            flow.write_json(reply, {"status":"success", "summary":"native done"})
            done = flow.concurrent_complete(run, "native", handoff["next"]["claim"], str(reply))
            self.assertEqual(done["status"], "success")
            again = flow.concurrent_complete(run, "native", handoff["next"]["claim"], str(reply))
            self.assertEqual(again["status"], "success")

    def test_failure_blocks_dependency_but_independent_work_can_finish(self):
        run = self.run_for([self.row("bad"), self.row("dependent", needs=["bad"]), self.row("other")])
        def worker(*args, **kwargs):
            return {"status":"failed" if args[6].name == "bad" else "success", "summary":"result"}
        with self.workers(worker):
            failed = flow.concurrent_advance(run, one_step=True, step_id="bad")
            self.assertEqual(failed["status"], "ready")
            self.assertFalse(failed["progress"]["all_settled"])
            done = flow.concurrent_advance(run, one_step=True, step_id="other")
            self.assertEqual(done["status"], "failed")
            self.assertTrue(done["progress"]["all_settled"])

    def test_saved_journal_is_reconciled_without_dispatch(self):
        run = self.run_for([self.row("a")])
        state = flow.read_state(run)
        state["steps"][0].update(status="running", started_at=flow.timestamp())
        flow.refresh_status(state)
        flow.write_json(run.state_path, state)
        flow.write_json(run.result_path("a"), {
            "id":"a", "status":"success", "summary":"already done", "outputs":[],
            "finished_at":flow.timestamp()})
        with self.workers(lambda *a, **k: self.fail("must not dispatch")):
            self.assertTrue(flow.query_result(run)["recovery_required"])
            self.assertEqual(flow.concurrent_advance(run, step_id="a")["status"], "success")

    def test_revision_preserves_success_and_archives_explicit_retry(self):
        run = self.run_for([self.row("done"), self.row("bad")])
        def worker(*args, **kwargs):
            return {"status":"failed" if args[6].name == "bad" else "success", "summary":"result"}
        with self.workers(worker):
            flow.concurrent_advance(run, one_step=True, step_id="done")
            flow.concurrent_advance(run, one_step=True, step_id="bad")
            spec = flow.saved_spec(flow.read_state(run))
            spec["steps"][1]["brief"] = "retry after diagnosing failure"
            spec["retry"] = ["bad"]
            revised = flow.revise(run, spec)
            self.assertEqual(revised["status"], "ready")
            self.assertEqual(revised["steps"][0]["status"], "success")
            self.assertTrue((run.step_dir("bad") / "attempts" / "1" / "step-result.json").is_file())
            self.assertFalse(run.result_path("bad").exists())
            spec["steps"][0]["brief"] = "illegally change completed work"
            with self.assertRaises(flow.Invalid):
                flow.revise(run, spec)

    def test_binding_override_and_agent_guard(self):
        row = self.row("a")
        row.update(agent="codex", model="gpt-6.2-sol", level="high")
        run = self.run_for([row])
        with self.workers(lambda *args, **kwargs: {"status":"success", "summary":"done"}):
            with self.assertRaises(flow.Invalid):
                flow.concurrent_advance(run, step_id="a", expected_agent="antigravity")
            result = flow.concurrent_advance(run, step_id="a", expected_agent="codex")
            self.assertEqual(result["steps"][0]["model"], "gpt-6.2-sol")

    def test_revision_recovers_after_cleanup_before_state_commit(self):
        run = self.run_for([self.row("bad")])
        with self.workers(lambda *args, **kwargs: {"status":"failed", "summary":"old failure"}):
            flow.concurrent_advance(run, step_id="bad")
            spec = flow.saved_spec(flow.read_state(run))
            spec["retry"] = ["bad"]
            original_write = flow.write_json
            def fail_commit(path, value):
                if path == run.state_path and value.get("revision_id"):
                    raise OSError("simulated power loss before state commit")
                return original_write(path, value)
            with patch.object(flow, "write_json", fail_commit):
                with self.assertRaises(OSError):
                    flow.revise(run, spec)
            self.assertTrue((run.path / "revision.json").exists())
            self.assertTrue((run.step_dir("bad") / "attempts" / "1" / "step-result.json").exists())
            recovered = flow.query_result(run)
            self.assertEqual(recovered["status"], "ready")
            self.assertEqual(recovered["steps"][0]["status"], "pending")
            self.assertFalse((run.path / "revision.json").exists())
            self.assertFalse(run.result_path("bad").exists())

    def test_unknown_write_sets_cannot_overlap(self):
        rows = [dict(self.row(sid, "implementation"), access="write") for sid in ("a", "b")]
        run = self.run_for(rows)
        state = flow.read_state(run)
        state["steps"][0]["status"] = "needs_claude"
        flow.refresh_status(state)
        flow.write_json(run.state_path, state)
        with self.workers(lambda *a, **k: self.fail("must serialize")):
            with self.assertRaises(flow.Invalid):
                flow.concurrent_advance(run, step_id="b")

    def test_declared_outputs_cannot_conflict_in_parallel(self):
        rows = [dict(self.row(sid, "implementation"), access="write", kind="files", outputs=[name])
                for sid, name in (("a", "out.txt"), ("b", "out.txt"))]
        with self.assertRaises(flow.Invalid):
            flow.validate({"steps":rows}, self.cfg, self.workspace)
        rows[1]["outputs"] = ["out.txt/child.txt"]
        with self.assertRaises(flow.Invalid):
            flow.validate({"steps":rows}, self.cfg, self.workspace)

    def historical_config(self):
        old = copy.deepcopy(self.cfg)
        models = old["agents"]["antigravity"]["models"]
        models["gemini-pro-orchestrator"] = ["medium"]
        models["gemini-pro-orchestrator-neon"] = ["high"]
        old["routes"]["vfx"]["level"] = "medium"
        old["routes"]["perf_analysis"]["level"] = "high"
        return old

    def test_old_codex_run_migrates_only_unrelated_agy_defaults(self):
        run = self.run_for([self.row("a")])
        state = flow.read_state(run)
        state.pop("concurrent")
        state["config"] = self.historical_config()
        flow.write_json(run.state_path, state)
        with self.workers(lambda *args, **kwargs: {"status":"success", "summary":"old run resumed"}):
            with flow.lock(run):
                flow.advance(run, state, one_step=True)
        self.assertEqual(flow.read_state(run)["status"], "success")
        self.assertEqual(flow.read_state(run)["config"], self.cfg)

    def test_affected_agy_binding_or_other_config_drift_rejects_migration(self):
        for rows in ([self.row("a", "vfx")], [self.row("a")]):
            run = self.run_for(rows)
            state = flow.read_state(run)
            state["config"] = self.historical_config()
            if rows[0]["route"] != "vfx":
                state["config"]["routes"]["review"]["level"] = "xhigh"
            with self.assertRaises(flow.Invalid):
                flow.compatible_config(state)

    def test_read_write_shared_paths_require_order_only_for_concurrent_runs(self):
        (self.workspace / "shared.txt").write_text("existing input", encoding="utf-8")
        writer = dict(self.row("writer", "implementation"), access="write", kind="files", outputs=["shared.txt"])
        reader = dict(self.row("reader"), refs=["shared.txt"])
        with self.assertRaises(flow.Invalid):
            flow.validate({"steps":[writer, reader]}, self.cfg, self.workspace)
        flow.validate({"steps":[writer, reader]}, self.cfg, self.workspace, concurrent=False)
        reader["needs"] = ["writer"]
        flow.validate({"steps":[writer, reader]}, self.cfg, self.workspace)
        reader["needs"] = []
        writer["needs"] = ["reader"]
        flow.validate({"steps":[writer, reader]}, self.cfg, self.workspace)

    def test_corrupt_revision_cannot_delete_records_of_unknown_steps(self):
        run = self.run_for([self.row("a")])
        replacement = copy.deepcopy(flow.read_state(run))
        replacement["revision_id"] = "test-corrupt-revision"
        ghost = run.path / "steps" / "ghost"
        ghost.mkdir()
        marker = ghost / "marker.txt"
        marker.write_text("preserve", encoding="utf-8")
        for sid in ("ghost", "../ghost"):
            flow.write_json(run.path / "revision.json", {"state":replacement, "resets":{sid:["marker.txt"]}})
            with self.assertRaises(flow.Invalid):
                flow.query_result(run)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
