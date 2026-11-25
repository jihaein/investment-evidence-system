"""Offline S4 recovery call-count and fallback tests."""
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from S4_investor_extraction import investor_mention_extraction as extraction
from S4_investor_extraction.extract_investor_mentions import run_s4
from S4_investor_extraction.openai_api_client import ProviderError
from S5_investor_arguments.build_investor_arguments import build
from S5_investor_arguments.graph_loader import Graph, read_json
from S5_investor_arguments.investor_argument_validation import verify


MESSAGE = "매출과 수요가 둘 다 늘면 이익도 늘 거야"
MENTIONS = [
    {"mid": "m1", "span": "매출", "node_id": "revenue", "alt_node_ids": [],
     "role": "grounds", "direction": "+", "horizon": "future", "certainty": "strong", "qualifiers": []},
    {"mid": "m2", "span": "수요", "node_id": "market_demand", "alt_node_ids": [],
     "role": "grounds", "direction": "+", "horizon": "future", "certainty": "strong", "qualifiers": []},
    {"mid": "m3", "span": "이익도 늘 거야", "node_id": "operating_profit", "alt_node_ids": [],
     "role": "claim", "direction": "+", "horizon": "future", "certainty": "strong", "qualifiers": []},
]
LINKS = [{"from": "m1", "to": "m3", "connective_span": "늘면"},
         {"from": "m2", "to": "m3", "connective_span": "늘면"}]


def output(members):
    return {"mentions": deepcopy(MENTIONS), "links": deepcopy(LINKS), "addresses": [],
            "logical_groups": [{"operator": "AND", "members": members,
                                "target_mid": "m3", "span": "둘 다"}]}


INTERMEDIATE_MESSAGE = ("1분기는 원래 좀 약하고, 실적이 올라오는 흐름이 있더라고요. "
                        "그래서 단기 분기 숫자보다는 연간 추세를 보려고 해요")


def intermediate_output(*, linked=True, operator="UNSPECIFIED"):
    mentions = [
        {"mid": "m1", "span": "1분기는 원래 좀 약하고", "node_id": None,
         "alt_node_ids": [], "role": "grounds", "direction": None,
         "horizon": "unspecified", "certainty": "strong", "qualifiers": ["1분기는"]},
        {"mid": "m2", "span": "실적이 올라오는 흐름이 있더라고요", "node_id": None,
         "alt_node_ids": [], "role": "claim", "direction": "+",
         "horizon": "past", "certainty": "strong", "qualifiers": []},
        {"mid": "m3", "span": "단기 분기 숫자보다는 연간 추세를 보려고 해요", "node_id": None,
         "alt_node_ids": [], "role": "claim", "direction": None,
         "horizon": "future", "certainty": "strong", "qualifiers": ["연간"]},
    ]
    links = [{"from": "m1", "to": "m3", "connective_span": "그래서"}]
    if linked:
        links.append({"from": "m2", "to": "m3", "connective_span": "그래서"})
    return {"mentions": mentions, "links": links, "addresses": [],
            "logical_groups": [{"operator": operator, "members": ["m1", "m2"],
                                "target_mid": "m3", "span": None}]}


class S4GroupRecoveryTests(unittest.TestCase):
    def run_case(self, replies):
        calls = []
        def generate(*_):
            calls.append(len(calls) + 1)
            value = replies[len(calls) - 1]
            if isinstance(value, Exception):
                raise value
            return json.dumps(value, ensure_ascii=False)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = run_s4(MESSAGE, generate, output_dir=Path(folder))
            result = json.loads(path.read_text(encoding="utf-8"))
        return calls, result

    def test_valid_first_response_uses_one_call(self):
        calls, result = self.run_case([output(["m1", "m2"])])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["logical_groups"][0]["members"], ["m1", "m2"])

    def test_invalid_group_member_is_not_replaced_by_an_ancestor(self):
        calls, result = self.run_case([output(["m1", "m3"]), output(["m1", "m2"])])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["logical_groups"][0]["members"], ["m1", "m2"])

    def test_duplicate_member_can_be_repaired_without_changing_membership(self):
        proposal = {"status": "repaired", "replacements": [{"group_index": 0,
                    "members": ["m1", "m2"]}], "reason": "중복 ID 제거"}
        calls, result = self.run_case([output(["m1", "m1", "m2"]), proposal])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["logical_groups"][0]["members"], ["m1", "m2"])

    def test_direct_linked_intermediate_claim_is_a_valid_group_member(self):
        calls = []
        expected = intermediate_output()
        def generate(*_):
            calls.append(1)
            return json.dumps(expected, ensure_ascii=False)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = run_s4(INTERMEDIATE_MESSAGE, generate, output_dir=Path(folder))
            result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, expected)

    def test_intermediate_claim_without_target_link_is_rejected(self):
        graph = extraction.load_graph()
        with self.assertRaisesRegex(extraction.InvalidExtraction, "logical_groups.members"):
            extraction.validate_result(intermediate_output(linked=False), INTERMEDIATE_MESSAGE,
                                       {node["id"] for node in extraction.node_dictionary(graph)})

    def test_s5_preserves_dual_role_group_without_promoting_claim_to_global_grounds(self):
        s4 = intermediate_output()
        graph = Graph(read_json(Path("S1_causal_graph/causal_graph.json")))
        cfg = read_json(Path("S5_investor_arguments/investor_argument_config.json"))
        cfg["llm_enabled"] = False
        result = build(s4, INTERMEDIATE_MESSAGE, graph, cfg, "dual-role", 1)
        verify(result, graph, s4)
        self.assertEqual(result["grounds"], ["m1"])
        self.assertEqual([c["mid"] for c in result["claims"]], ["m2", "m3"])
        self.assertEqual(result["logical_groups"][0]["members"], ["m1", "m2"])

    def test_operator_is_preserved_for_valid_direct_linked_intermediate(self):
        value = intermediate_output(operator="UNSPECIFIED")
        graph = extraction.load_graph()
        extraction.validate_result(value, INTERMEDIATE_MESSAGE,
                                   {node["id"] for node in extraction.node_dictionary(graph)})
        self.assertEqual(value["logical_groups"][0]["operator"], "UNSPECIFIED")

    def test_non_group_error_skips_repair_and_regenerates_once(self):
        invalid = output(["m1", "m2"])
        invalid["mentions"][0] = dict(invalid["mentions"][0], node_id="missing-node")
        calls, result = self.run_case([invalid, output(["m1", "m2"])])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["mentions"][0]["node_id"], "revenue")

    def test_provider_timeout_does_not_trigger_an_outer_s4_regeneration(self):
        calls = []
        def generate(*_):
            calls.append(1)
            raise ProviderError("connection_or_timeout")
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            with self.assertRaisesRegex(RuntimeError, "connection_or_timeout"):
                run_s4(MESSAGE, generate, output_dir=Path(folder))
            diagnostic = json.loads((Path(folder) / "s4_failure_diagnostic.json").read_text(encoding="utf-8"))
            self.assertFalse(diagnostic["raw_preserved"])
            self.assertFalse((Path(folder) / "s4_failure_raw.json").exists())
        self.assertEqual(len(calls), 1)

    def test_group_failure_diagnostic_records_index_role_and_relation_without_raw(self):
        invalid = output(["m1", "m3"])
        def generate(*_):
            return json.dumps(invalid, ensure_ascii=False)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with self.assertRaises(RuntimeError):
                run_s4(MESSAGE, generate, output_dir=root)
            diagnostic = json.loads((root / "s4_failure_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["groups"][0]["group_index"], 0)
            member = next(row for row in diagnostic["groups"][0]["members"] if row["mid"] == "m3")
            self.assertEqual(member["role"], "claim")
            self.assertFalse(member["direct_to_target"])
            self.assertEqual(diagnostic["repair_phase"], "diagnose")
            self.assertIn("group_index=0", diagnostic["repair_error"])
            self.assertIsNotNone(diagnostic["raw_response_sha256"])
            self.assertFalse((root / "s4_failure_raw.json").exists())

    def test_failure_raw_can_only_be_preserved_by_explicit_opt_in(self):
        invalid = output(["m1", "m3"])
        def generate(*_):
            return json.dumps(invalid, ensure_ascii=False)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with self.assertRaises(RuntimeError):
                run_s4(MESSAGE, generate, output_dir=root, preserve_failure_raw=True)
            raw = json.loads((root / "s4_failure_raw.json").read_text(encoding="utf-8"))
            self.assertEqual(len(raw["responses"]), 2)
            self.assertTrue(json.loads(raw["responses"][0]["raw_response"])["logical_groups"])


if __name__ == "__main__":
    unittest.main()
