"""Offline selection tests on the real S1 graph. Never calls a provider."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from S5_investor_arguments.build_investor_arguments import build
from S5_investor_arguments.run_investor_arguments import GRAPH, HERE, Graph, read_json
from S5_investor_arguments.investor_argument_validation import verify

D = Path(__file__).parent / "fixtures" / "s5"


def reply(*chosen):
    return json.dumps([{"id": f"C{i}", "selected": i in chosen, "reason": f"reason {i}"}
                       for i in (1, 2, 3)])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.s4 = read_json(D / "investor_mentions.json")
        self.graph = Graph(read_json(GRAPH))
        self.message = (D / "investor_message.txt").read_text(encoding="utf8")
        self.cfg = read_json(HERE / "investor_argument_config.json")
        self.api = Mock(return_value=reply(3))

    def execute(self):
        self.out = build(self.s4, self.message, self.graph, self.cfg, "mock", 1, generate=self.api)
        verify(self.out, self.graph, self.s4)
        return self.out["searches"]["Q1"]

    def test_link_selects_third_candidate_with_ambiguous_primary(self):
        q = self.execute()
        self.api.assert_called_once()
        payload = json.loads(self.api.call_args.args[1])
        self.assertNotIn("alt_node_ids", payload["mentions"][0])
        self.assertTrue(all(c["nodes"][0]["id"] == "revenue" for c in payload["candidates"]))
        self.assertEqual(q["selection"]["selected"], ["C3"])
        self.assertEqual(self.out["mappings"]["m1"]["status"], "ambiguous")
        self.assertTrue(self.out["paths"]["P1"]["tentative"])

    def test_chain(self):
        self.s4["links"] = []
        self.assertEqual(self.execute()["selection"]["method"], "llm")
        self.assertEqual(self.out["paths"]["P1"]["origin"], "chain")

    def test_no_path_with_alternate_witness(self):
        self.s4["mentions"][0].update(node_id="market_equity", alt_node_ids=["revenue"])
        q = self.execute()
        self.api.assert_not_called()
        self.assertEqual(q["status"], "no_path")
        self.assertEqual(q["alternatives"][0]["status"], "candidate_only")
        self.assertFalse(q["path_ids"])

    def test_missing_primary(self):
        self.s4["mentions"][0].update(node_id=None, alt_node_ids=["revenue"])
        self.assertFalse(self.execute()["path_ids"])
        self.api.assert_not_called()

    def test_single(self):
        self.s4["mentions"][1]["node_id"] = "operating_profit"
        self.assertEqual(self.execute()["selection"]["selected"], ["C1"])
        self.api.assert_not_called()

    def test_disabled_python_baseline(self):
        self.cfg["llm_enabled"] = False
        self.assertEqual(self.execute()["selection"]["selected"], ["C1", "C2"])
        self.api.assert_not_called()

    def test_all_false_is_deferred_without_automatic_paths(self):
        self.api = Mock(return_value=reply())
        q = self.execute()
        self.api.assert_called_once()
        self.assertEqual(q["status"], "selection_deferred")
        self.assertEqual(q["selection"]["method"], "llm")
        self.assertEqual(q["selection"]["outcome"], "deferred")
        self.assertFalse(q["selection"]["fallback"])
        self.assertEqual(q["selection"]["selected"], [])
        self.assertFalse(q["path_ids"])
        self.assertFalse(self.out["paths"])
        self.assertFalse(self.out["side_checks"])
        self.assertEqual(self.out["investor_mentions"], self.s4)
        self.assertTrue(any(x.get("reason") == "selection_deferred" and x.get("search_id") == "Q1"
                            for x in self.out["unresolved"]))

    def test_invalid_or_provider_failure_stays_unresolved_without_automatic_paths(self):
        for response in [reply(3).replace("C3", "C9"), reply(1, 2, 3), "bad JSON",
                         TimeoutError("private provider detail")]:
            with self.subTest(response=type(response).__name__):
                self.api = Mock(side_effect=[response])
                q = self.execute()
                audit = q["selection"]
                self.api.assert_called_once()
                self.assertFalse(audit["fallback"])
                self.assertEqual(audit["method"], "none")
                self.assertIn(audit["outcome"], {"provider_error", "invalid_response"})
                self.assertTrue(audit["error_reason"])
                self.assertEqual(audit["selected"], [])
                self.assertEqual(q["status"], "selection_error")
                self.assertFalse(q["path_ids"])
                self.assertFalse(self.out["paths"])
                self.assertFalse(self.out["side_checks"])
                self.assertTrue(any(x.get("reason") == "selection_error" for x in self.out["unresolved"]))
                self.assertNotIn("private provider detail", str(audit))
