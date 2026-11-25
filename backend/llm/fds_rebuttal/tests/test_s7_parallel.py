"""Offline S7 scheduling tests. No requests are sent to an LLM provider."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from S6_S7_S8_tagging_validation import tagging_and_assessment_pipeline as p
from S4_investor_extraction import openai_api_client as openai_api


class S7ParallelTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="s7-parallel-test-", dir=p.ROOT)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "draft.json"
        self.draft = dict(schema_version="S6.1", session_id="parallel-test", turn_id=1,
                          stock_code="123456", period="2025-2", as_of="2025-06-01",
                          graph_version="test", graph_schema_sha256="test-hash")
        self.path.write_text(json.dumps(self.draft), encoding="utf-8")
        self.context = (dict(session_id="parallel-test", turn_id=1), dict(company_id="123456"),
                        {}, {}, SimpleNamespace(version="test", fingerprint="test-hash"), {}, {})
        self.items = [dict(target=f"node:n{i}", use_id=f"mention:m{i}", target_origin="explicit",
                           mid=f"m{i}", path_id=None, side_id=None, link_id=None,
                           pending_reasons=["interpret_qualifiers"], available_refs=[f"fact-{i}"],
                           investor=dict(tentative=False)) for i in range(8)]

    def reply(self, item, **changes):
        value = dict(status="proposed", target=item["target"], use_id=item["use_id"],
                     tag="unknown", relation=None, reason="Offline fixture", evidence_refs=item["available_refs"])
        value.update(changes)
        return json.dumps(value)

    def run_s7(self, generate, *, items=None, **kwargs):
        with patch.object(p, "check_output"), patch.object(p, "select_s7", return_value=(
                deepcopy(self.items if items is None else items), [])):
            return p.run_s7(self.path, self.folder / f"output-{len(list(self.folder.iterdir()))}",
                            context=self.context, model="gpt-6.1-terra", generate=generate, **kwargs)

    def test_four_requests_overlap_and_out_of_order_completion_preserves_input_order(self):
        barrier, release_first, guard = Barrier(4), Event(), Lock()
        active, peak, completed = 0, 0, []

        def generate(item, model, timeout, retries):
            nonlocal active, peak
            index = int(item["mid"][1:])
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                self.assertEqual((model, timeout, retries), ("gpt-6.1-terra", 30, 1))
                if index < 4:
                    barrier.wait(timeout=5)
                if index == 0:
                    self.assertTrue(release_first.wait(timeout=5))
                with guard:
                    completed.append(index)
                if index == 3:
                    release_first.set()
                return self.reply(item)
            finally:
                with guard:
                    active -= 1

        path, result = self.run_s7(generate)
        self.assertEqual(peak, 4)
        self.assertLess(completed.index(3), completed.index(0))
        self.assertEqual([row["use_id"] for row in result["tags"]], [row["use_id"] for row in self.items])
        self.assertEqual(result["metadata"], dict(selected_count=8, api_call_count=8, enabled=True,
                                                  max_workers=4, effective_workers=4))
        self.assertEqual(result["failed_items"], [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)

    def test_worker_failure_invalid_refs_and_defer_do_not_drop_other_results(self):
        items = deepcopy(self.items)
        items[5]["target"] = "cc:edge:cc-test"

        def generate(item, *_):
            index = int(item["mid"][1:])
            if index == 0:
                raise TimeoutError("offline timeout")
            if index == 1:
                return "invalid json"
            if index == 2:
                return self.reply(item, evidence_refs=["another-items-ref"])
            if index == 3:
                return self.reply(item, status="deferred")
            if index == 5:
                return self.reply(item, tag="supported")
            return self.reply(item)

        _, result = self.run_s7(generate, items=items, max_workers=8)
        self.assertEqual([row["use_id"] for row in result["tags"]], ["mention:m4", "mention:m6", "mention:m7"])
        self.assertEqual([row["use_id"] for row in result["failed_items"]],
                         ["mention:m0", "mention:m1", "mention:m2", "mention:m3", "mention:m5"])
        self.assertEqual([row["reason"] for row in result["failed_items"]],
                         ["api_or_parse_error", "invalid_response", "invalid_response", "model_deferred", "invalid_response"])
        self.assertEqual(result["metadata"]["api_call_count"], len(items))

    def test_serial_and_parallel_have_identical_item_results(self):
        def generate(item, *_):
            return self.reply(item, status="deferred" if item["mid"] in ("m2", "m5") else "proposed")
        _, serial = self.run_s7(generate, max_workers=1)
        _, parallel = self.run_s7(generate, max_workers=4)
        _, eight = self.run_s7(generate, max_workers=8)
        for section in ("tags", "failed_items", "not_escalated"):
            self.assertEqual(serial[section], parallel[section])
            self.assertEqual(serial[section], eight[section])
        self.assertEqual(serial["metadata"]["api_call_count"], parallel["metadata"]["api_call_count"])
        self.assertEqual(serial["metadata"]["api_call_count"], eight["metadata"]["api_call_count"])

    def test_eight_requests_overlap_and_keep_input_identity(self):
        barrier = Barrier(8)
        active, peak = 0, 0
        guard = Lock()

        def generate(item, *_):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                time.sleep((7 - int(item["mid"][1:])) * .002)
                return self.reply(item)
            finally:
                with guard:
                    active -= 1

        _, result = self.run_s7(generate, max_workers=8)
        self.assertEqual(peak, 8)
        self.assertEqual([r["use_id"] for r in result["tags"]], [r["use_id"] for r in self.items])
        self.assertEqual(result["metadata"]["effective_workers"], 8)

    def test_diagnostic_callbacks_are_serialized_and_keep_request_identity(self):
        callback_lock, events, overlaps = Lock(), [], []

        def diagnostic(event):
            if not callback_lock.acquire(blocking=False):
                overlaps.append(event)
                return
            try:
                time.sleep(.001)
                events.append(event)
            finally:
                callback_lock.release()

        _, result = self.run_s7(lambda item, *_: self.reply(item), diagnostic=diagnostic)
        self.assertFalse(overlaps)
        self.assertEqual(len(events), 2 * len(self.items))
        for item in self.items:
            trace = [e for e in events if e["use_id"] == item["use_id"]]
            self.assertEqual([e["outcome"] for e in trace], ["started", "proposed"])
            self.assertEqual(len({e["request_id"] for e in trace}), 1)
            self.assertTrue(all(e["target"] == item["target"] and e["available_refs"] == item["available_refs"] for e in trace))
        self.assertEqual(len({e["request_id"] for e in events}), len(self.items))
        self.assertEqual(len(result["tags"]), len(self.items))

    def test_disabled_and_empty_runs_do_not_create_workers_or_call_generate(self):
        with patch.object(p, "ThreadPoolExecutor", side_effect=AssertionError("unexpected executor")):
            _, disabled = self.run_s7(lambda *_: self.fail("unexpected provider call"), enabled=False)
            _, empty = self.run_s7(lambda *_: self.fail("unexpected provider call"), items=[])
        self.assertEqual(disabled["metadata"]["api_call_count"], 0)
        self.assertEqual(disabled["metadata"]["effective_workers"], 0)
        self.assertEqual(len(disabled["failed_items"]), len(self.items))
        self.assertTrue(all(row["reason"] == "llm_disabled" for row in disabled["failed_items"]))
        self.assertEqual(empty["metadata"]["effective_workers"], 0)
        self.assertEqual(empty["metadata"]["api_call_count"], 0)

    def test_worker_limit_rejects_values_outside_one_through_four(self):
        for value in (0, 9, -1, True, 2.5, "4"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_workers"):
                self.run_s7(lambda *_: self.fail("unexpected provider call"), max_workers=value)

    def test_identical_evaluation_is_called_once_and_revalidated_per_use(self):
        items = deepcopy(self.items[:2])
        items[1] = deepcopy(items[0])
        items[1].update(use_id="path:P1:N1", path_id="P1")
        calls = []

        def generate(item, *_):
            calls.append(item["use_id"])
            return self.reply(item)

        _, result = self.run_s7(generate, items=items)
        self.assertEqual(calls, [items[0]["use_id"]])
        self.assertEqual(result["metadata"]["api_call_count"], 1)
        self.assertEqual([x["use_id"] for x in result["tags"]], [x["use_id"] for x in items])
        self.assertEqual([x["path_id"] for x in result["tags"]], [None, "P1"])
        self.assertEqual(result["failed_items"], [])

    def test_parallel_workers_share_only_the_active_request_recorder(self):
        def generate(item, *_):
            openai_api._record_call({"model": "offline", "use_id": item["use_id"], "seconds": 0})
            return self.reply(item)

        with openai_api.capture_calls() as recorder:
            self.run_s7(generate)
        self.assertEqual({row["use_id"] for row in recorder.snapshot()},
                         {row["use_id"] for row in self.items})


if __name__ == "__main__":
    unittest.main()
