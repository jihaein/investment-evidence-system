"""analyze(분석 단계) 점검. 실제 OpenAI 호출 없이 가짜 응답으로 본다.
보낼 내용의 모양, 답 형식(strict), 코드 확인 규칙, 시험 사례 채점이 맞는지 확인한다."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "llm"))
import analyze as an  # noqa: E402
import build_pack as bp  # noqa: E402

CASES = json.loads(an.CASES_PATH.read_text(encoding="utf-8"))
CASE1 = CASES[0]                                              # 대한광통신: 재무안정성 반박, 시장기대 지지


class FakeClient:
    def __init__(self, payload, refusal=None):
        self.payloads = payload if isinstance(payload, list) else [payload]   # 여러 개면 부를 때마다 차례로
        self.refusal, self.calls = refusal, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls), len(self.payloads)) - 1]
        msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), refusal=self.refusal)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture(scope="module")
def pack1():
    return bp.build_pack(CASE1["stock_code"], CASE1["buy_date"], CASE1["eval_date"], CASE1["opinion"], include_dupont=False)


def good_answer():
    return {
        "claims": [
            {"id": "C1", "text": "재무 구조가 탄탄하다", "type": "재무안정성", "models_to_check": ["fscore"],
             "verdict": "반박", "evidence_ids": ["FS-1", "FS-2"],
             "reason": "F-Score가 7점에서 3점으로 4점 하락해 위험점수 1점이다(FS-1, FS-2)."},
            {"id": "C2", "text": "시장이 이익 체력을 더 높게 평가할 것이다", "type": "시장기대", "models_to_check": ["ohlson"],
             "verdict": "지지", "evidence_ids": ["OH-1"], "reason": "ω 상태가 '최하'에서 '높음'으로 올라갔다(OH-1)."},
        ],
        "injection_detected": False,
        "summary": "재무 안정성 주장은 F-Score 하락으로 반박되고, 시장 기대 주장은 Ohlson ω 상승으로 지지된다.",
        "data_limits": ["DuPont은 계산되지 않음"],
    }


def walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_objects(v)


def test_schema_is_strict_and_only_allows_pack_ids(pack1):
    schema = an.assessment_schema(pack1)
    for obj in walk_objects(schema):                          # OpenAI strict 규칙: 추가 칸 금지, 모든 칸 필수
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    ids = schema["properties"]["claims"]["items"]["properties"]["evidence_ids"]["items"]["enum"]
    assert ids == [e["id"] for e in pack1["evidence_index"]]


def test_messages_keep_the_opinion_in_its_own_box(pack1):
    system, user = an.build_messages(pack1)
    assert "{{" not in system["content"] and "| 재무안정성 |" in system["content"]
    assert f"<investor_opinion>\n{CASE1['opinion']}\n</investor_opinion>" in user["content"]
    assert user["content"].count(CASE1["opinion"]) == 1        # 묶음 안에 원문을 한 번 더 넣지 않는다
    assert '"created_at"' not in user["content"]


def test_good_answer_passes_and_request_is_strict_json(pack1):
    client = FakeClient(good_answer())
    result = an.analyze(pack1, model="test-model", client=client, save=False)
    assert result["issues"] == []
    fmt = client.calls[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert client.calls[0]["model"] == "test-model" and result["tokens"]["total_tokens"] == 150


def test_validation_catches_made_up_numbers_bad_words_and_wrong_verdicts(pack1):
    bad = good_answer()
    bad["claims"][0]["reason"] = "F-Score가 9.9점 떨어졌다(FS-1)."                        # 묶음에 없는 숫자
    bad["claims"][1]["evidence_ids"] = ["OH-1", "FS-1"]                                  # 확인 대상이 아닌 모델 근거
    bad["claims"].append({"id": "C3", "text": "재무가 좋다", "type": "재무안정성", "models_to_check": ["fscore"],
                          "verdict": "판단보류", "evidence_ids": [], "reason": "판단할 수 없다."})  # F-Score는 점수가 있음
    bad["claims"].append({"id": "C4", "text": "이익률이 오른다", "type": "수익성", "models_to_check": ["dupont", "ohlson"],
                          "verdict": "지지", "evidence_ids": ["DP-1"], "reason": "DuPont을 보면 그렇다(DP-1)."})  # 계산 안 됨을 근거로
    bad["summary"] = "이 종목은 매수를 추천한다."
    issues = "\n".join(an.validate_assessment(bad, pack1))
    assert "없는 숫자 ['9.9']" in issues
    assert "오류: C2 확인 대상이 아닌 모델의 근거를 씀: ['fscore']" in issues
    assert "C3 '판단보류'인데" in issues
    assert "C4 '지지'인데 점수가 난 모델의 근거가 없음" in issues
    assert "금지 표현 '매수를 추천'" in issues


def test_wrong_hold_is_repaired_by_asking_again(pack1):
    wrong = good_answer()
    wrong["claims"][1].update(verdict="판단보류", evidence_ids=[], reason="판단하지 않았다.")   # Ohlson은 점수가 있음
    client = FakeClient([wrong, good_answer()])
    result = an.analyze(pack1, model="m", client=client, save=False)
    assert result["attempts"] == 2 and result["issues"] == []
    assert any("판단보류" in i for i in result["first_issues"])
    repair = client.calls[1]["messages"][-1]["content"]
    assert "다시 답하세요" in repair and "C2 '판단보류'인데" in repair
    assert result["tokens"]["total_tokens"] == 300


def test_warnings_alone_do_not_trigger_a_retry(pack1):
    answer = good_answer()
    answer["claims"][0]["reason"] += " 가격 경보도 있었다."                      # 다른 모델 이름만 나옴 → 경고
    client = FakeClient([answer, good_answer()])
    result = an.analyze(pack1, model="m", client=client, save=False)
    assert result["attempts"] == 1 and len(client.calls) == 1
    assert any(i.startswith("경고: C1 설명에 확인 대상이 아닌 모델이 나옴") for i in result["issues"])


def test_missed_injection_is_warned():
    pack = bp.build_pack("010170", "2016-06-30", "2019-06-28", CASES[3]["opinion"], include_dupont=False)
    answer = {"claims": [{"id": "C1", "text": "영업이익률이 좋아질 것", "type": "수익성",
                          "models_to_check": ["dupont", "ohlson"], "verdict": "지지", "evidence_ids": ["OH-2"],
                          "reason": "RI Spread가 상승했다(OH-2)."}],
              "injection_detected": False, "summary": "수익성 주장은 지지된다.", "data_limits": []}
    assert any("injection_detected가 false" in i for i in an.validate_assessment(answer, pack))


def test_refusal_and_missing_key_give_clear_errors(pack1, monkeypatch):
    with pytest.raises(RuntimeError, match="거절"):
        an.analyze(pack1, model="m", client=FakeClient(good_answer(), refusal="안 됨"), save=False)
    monkeypatch.setattr(an, "load_env", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        an.call_openai([], {}, "m")


def test_case_scoring(pack1):
    result = {"assessment": good_answer(), "issues": []}
    assert all(ok for ok, _ in an.evaluate_case(CASE1, result))
    wrong = good_answer()
    wrong["claims"][0]["verdict"] = "지지"
    assert not all(ok for ok, _ in an.evaluate_case(CASE1, {"assessment": wrong, "issues": []}))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_cases_only_point_at_real_evidence(case):
    pack = bp.build_pack(case["stock_code"], case["buy_date"], case["eval_date"], case["opinion"],
                         include_dupont=case.get("include_dupont", True))
    ids = {e["id"] for e in pack["evidence_index"]}
    for exp in case["expected"]:
        assert set(exp["types"]) <= set(an.CLAIM_TYPES)
        assert set(exp["verdicts"]) <= set(an.VERDICTS)
        assert set(exp["cite_any"]) <= ids


def test_dry_run_needs_no_key():
    out = subprocess.run([sys.executable, str(ROOT / "backend" / "llm" / "analyze.py"), "010170", "2016-06-30",
                          "2019-06-28", "--opinion", "재무가 탄탄하다", "--no-dupont", "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})   # 윈도우 콘솔 인코딩과 무관하게 읽는다
    assert out.returncode == 0, out.stderr
    assert "[크기]" in out.stdout
