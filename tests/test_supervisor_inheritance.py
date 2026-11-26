"""Regression coverage for session inheritance and fixed supervisor settings."""
import copy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import flow
import sync_supervisor


class SupervisorInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = flow.config()

    def test_sync_switches_between_inherited_and_fixed_settings(self):
        original = "---\nname: steam\nmodel: opus\neffort: high\n---\nBody\n"
        with tempfile.TemporaryDirectory(prefix="steam-supervisor-test-") as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text(original, encoding="utf-8")
            _, inherited = sync_supervisor.desired_text(root, self.cfg)
            self.assertIn("model: inherit\n", inherited)
            self.assertNotIn("effort:", inherited)
            self.assertTrue(inherited.endswith("---\nBody\n"))
            skill.write_text(inherited, encoding="utf-8")
            sync_supervisor.check_synced(root, self.cfg)
            fixed = copy.deepcopy(self.cfg)
            fixed["supervisor"].update(model="sonnet", level="medium")
            _, restored = sync_supervisor.desired_text(root, fixed)
            self.assertIn("model: sonnet\neffort: medium\n", restored)

    def test_fixed_values_still_require_a_configured_combination(self):
        self.cfg["supervisor"].update(model="opus", level="low")
        with patch.object(flow, "read_json", return_value=self.cfg):
            with self.assertRaises(flow.Invalid):
                flow.config()

    def test_model_and_effort_can_inherit_independently(self):
        with tempfile.TemporaryDirectory(prefix="steam-supervisor-test-") as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text("---\nname: steam\n---\nBody\n", encoding="utf-8")
            for model, level in (("inherit", "high"), ("sonnet", "inherit")):
                with self.subTest(model=model, level=level):
                    cfg = copy.deepcopy(self.cfg)
                    cfg["supervisor"].update(model=model, level=level)
                    with patch.object(flow, "read_json", return_value=cfg):
                        self.assertEqual(flow.config(), cfg)
                    _, text = sync_supervisor.desired_text(root, cfg)
                    self.assertIn(f"model: {model}\n", text)
                    self.assertEqual("effort:" in text, level != "inherit")

    def test_environment_check_keeps_inherited_claude_routes_native(self):
        checked = []

        def check_worker(name, agent, models, workspace):
            checked.append(name)
            return {"status":"ok"}

        with patch.dict(sys.modules, {"workers":types.SimpleNamespace(check_worker=check_worker)}):
            result = flow.check(self.cfg, Path.cwd())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["agents"]["claude"]["status"], "native")
        self.assertNotIn("claude", checked)

    def test_inherited_supervisor_does_not_claim_a_fixed_worker_model(self):
        fixed = {"model":"claude-example", "level":"high"}
        self.assertTrue(flow.needs_claude_cli("claude-example", "high", self.cfg["supervisor"]))
        self.assertFalse(flow.needs_claude_cli("claude-example", "high", fixed))
        self.assertFalse(flow.needs_claude_cli("sonnet", "medium", self.cfg["supervisor"]))

    def test_default_upgrade_preserves_saved_step_dispatch(self):
        saved = copy.deepcopy(self.cfg)
        saved["supervisor"].update(model="opus", level="high")
        state = {"config":saved, "steps":[{"route":"project_analysis"}, {"route":"documentation"}]}
        with patch.object(flow, "config", return_value=self.cfg):
            self.assertEqual(flow.compatible_config(state), self.cfg)

    def test_other_supervisor_drift_remains_rejected(self):
        saved = copy.deepcopy(self.cfg)
        saved["supervisor"].update(model="sonnet", level="medium")
        state = {"config":saved, "steps":[{"route":"project_analysis"}]}
        with patch.object(flow, "config", return_value=self.cfg):
            with self.assertRaises(flow.Invalid):
                flow.compatible_config(state)


if __name__ == "__main__":
    unittest.main()
