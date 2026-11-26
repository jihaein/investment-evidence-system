"""Deterministic evidence checks. These tests do not generate dialogue."""
from copy import deepcopy
import unittest

from S10.live_scenarios import SEVENTH, fixture
from S10.run import fact_value, load_context, prepare_request, render_draft, response_strategy, validate


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = load_context(SEVENTH)
        cls.request = prepare_request(cls.context)
        cls.quote, = cls.request["fact_quotes"].values()

    def check_text(self, message):
        validate(dict(character_message=message, asked_cc_id=None, emotion_code="thinking"),
                 self.context, request=self.request)

    def test_actual_records(self):
        self.check_text(self.quote["text"])
        self.assertEqual(len(self.quote["fact_ids"]), 2)
        for fid in self.quote["fact_ids"]:
            row = self.context["facts"][fid]
            self.assertEqual((row["company_id"], row["node_id"]), ("009730", "revenue"))
            self.assertIn(f"{row['period_end']} 기준 {row['value']:,}원", self.quote["text"])

    def test_wrong_amount_scale(self):
        with self.assertRaisesRegex(ValueError, "invalid_amount"):
            self.check_text(self.quote["text"] + " 금액은 2,965억 원대였어.")
        with self.assertRaisesRegex(ValueError, "invalid_amount"):
            self.check_text(self.quote["text"] + " ① 기준으로 늘었어.")

    def test_swapped_periods(self):
        quote = self.quote["text"]
        swapped = quote.replace("2024-12-31", "TEMP").replace("2025-06-30", "2024-12-31").replace("TEMP", "2025-06-30")
        with self.assertRaisesRegex(ValueError, "missing_required_evidence"):
            self.check_text(swapped)
        with self.assertRaisesRegex(ValueError, "invalid_fact_period"):
            self.check_text(quote + " 2025-06-30 기준 29,659,386,850원이었어.")

    def test_company_and_missing_id(self):
        context = deepcopy(self.context)
        fid = self.quote["fact_ids"][0]
        context["facts"][fid]["company_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "unsupported_fact"):
            prepare_request(context)
        del context["facts"][fid]
        with self.assertRaisesRegex(ValueError, "missing_required_evidence"):
            prepare_request(context)

    def test_percentage_rounding(self):
        self.assertEqual(fact_value(dict(unit="fraction", value="-0.01555")), "약 -1.56%")

    def test_unknown_unit(self):
        with self.assertRaisesRegex(ValueError, "unsupported_fact"):
            fact_value(dict(unit="unknown", node_id="revenue", value=100))

    def test_target_scope(self):
        scope = self.request["rebuttal_boundaries"][0]
        self.assertEqual((scope["span"], scope["horizon"], scope["tag"]),
                         ("최근 매출 증가", "past", "contradicted"))
        self.assertEqual(len(scope["fact_ids"]), 3)

    def test_marker_rendering_and_budget(self):
        response = dict(claim_force="other", link_status="not_applicable",
                        statements=["그 기간의 매출 증가 주장은 기록과 달라."],
                        repair_question=None, emotion_code="thinking", asked_cc_id=None)
        result = render_draft(response, self.context, self.request)
        self.assertIn(self.quote["text"], result["character_message"])
        limited = render_draft(dict(response, statements=["사실 정정이야.", "미래도 틀렸어."]),
                               self.context, self.request)
        self.assertNotIn("미래도", limited["character_message"])
        for statements in [["매출은 123원이야."], ["[[BAD]]"], ["내 친구가 샀다고 했지."]]:
            with self.subTest(statements=statements), self.assertRaises(ValueError):
                render_draft(dict(response, statements=statements), self.context, self.request)
        trimmed = render_draft(dict(response, statements=["하나.", "둘.", "셋.", "넷."]),
                               self.context, self.request)
        self.assertLessEqual(len(trimmed["character_message"].split(". ")), 4)

    def test_question_marker(self):
        context = fixture("비용이 늘었어.", "일회성이면 회복될 수 있어.")
        context["question_candidates"] = [dict(cc_id="cost", text="비용이 일회성이었는지")]
        request = prepare_request(context)
        response = dict(claim_force="other", link_status="not_applicable",
                        statements=["네 설명대로라면 비용의 지속 여부가 중요해."],
                        repair_question=None, emotion_code="curious", asked_cc_id="cost")
        result = render_draft(response, context, request)
        self.assertEqual(result["asked_cc_id"], "cost")
        self.assertIn(request["question_candidates"][0]["question"], result["character_message"])
        self.assertIsNone(render_draft(dict(response, asked_cc_id=None), context, request)["asked_cc_id"])
        ignored = render_draft(dict(response, repair_question="왜 그런지 알려줄래?"), context, request)
        self.assertNotIn("왜 그런지", ignored["character_message"])

    def test_leap_contract(self):
        context = fixture("내 친구가 이 주식을 샀어.", "기업가치는 무조건 오를 거야.", outside=True)
        request = prepare_request(context)
        good = dict(claim_force="necessity", link_status="missing",
                    statements=["네 친구의 매수만으로 회사 가치 상승이 보장되지는 않아."],
                    repair_question="그 매수가 회사 가치에 어떻게 영향을 준다고 생각해?",
                    emotion_code="thinking", asked_cc_id=None)
        self.assertIn("어떻게", render_draft(good, context, request)["character_message"])
        with self.assertRaisesRegex(ValueError, "missing-link question"):
            render_draft(dict(good, repair_question=None), context, request)

    def test_plausible_uses_safe_attribution(self):
        context = fixture("외국인 매수가 이어진다고 들었어.", "주가가 오를 가능성이 있어.", outside=True)
        request = prepare_request(context)
        response = dict(claim_force="possibility", link_status="possible_unverified",
                        statements=["외국인 매수가 이어진다면 주가 상승 가능성은 생각해볼 수 있어."], repair_question=None,
                        emotion_code="curious", asked_cc_id=None)
        message = render_draft(response, context, request)["character_message"]
        self.assertIn("네 말이 맞다면, 외국인 매수가 이어진다면", message)
        self.assertIn("확인된 기업 근거가 없어", message)
        self.assertEqual(response_strategy(response, request), "plausible")
        already_conditional = render_draft(dict(response, statements=[
            "네 말이 맞다면 외국인 매수가 이어질 때 주가가 오를 가능성이 있어."]),
            context, request)["character_message"]
        self.assertEqual(already_conditional.count("네 말이 맞다면"), 1)

    def test_question_identity_and_history(self):
        context = fixture("비용이 늘었어.", "일회성이면 회복될 수 있어.")
        context.update(asked_before=["orders"], question_candidates=[
            dict(cc_id="cost", text="비용 증가가 일회성이었는지"),
            dict(cc_id="orders", text="주문이 늘었는지")])
        request = prepare_request(context)
        candidate, = request["question_candidates"]
        self.assertEqual(candidate["cc_id"], "cost")
        result = dict(character_message=candidate["question"], emotion_code="curious", asked_cc_id="cost")
        validate(result, context, request=request)
        for text, asked in [(candidate["question"], None), ("왜 그렇지?", "cost"),
                            ("주문이 늘었는지 확인했어?", "orders"), ("주문이 늘었는지 확인했어?", None)]:
            with self.subTest(text=text, asked=asked), self.assertRaisesRegex(ValueError, "invalid_question"):
                validate(dict(result, character_message=text, asked_cc_id=asked), context, request=request)


if __name__ == "__main__":
    unittest.main()
