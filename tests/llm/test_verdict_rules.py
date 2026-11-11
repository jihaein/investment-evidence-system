"""판정 순서와 방향 규칙 점검. 실제 OpenAI 호출 없이 본다.
- 코드가 만든 판정 안내(judging_guide)가 모델 상태와 맞는지
- 답 형식에서 판정이 설명·근거 방향 뒤에 오는지
- 주장 방향·근거 방향과 판정이 어긋나면 오류로 잡는지 (예: 주장 나쁨 + 근거 좋음 = 반박인데 지지)"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "llm"))
import analyze as an  # noqa: E402
import build_pack as bp  # noqa: E402

DAEHAN = ("010170", "2016-06-30", "2019-06-28")
OPINION = "원자재 가격이 올라서 영업이익률이 떨어질 것 같다. 재무 구조도 탄탄하다."


@pytest.fixture(scope="module")
def pack():
    return bp.build_pack(*DAEHAN, opinion_text=OPINION, include_dupont=False)   # DuPont 계산 안 됨, Ohlson 점수 있음


def rows(guide):
    return {line.split("|")[1].strip(): [x.strip() for x in line.split("|")[2:-1]] for line in guide.splitlines()[2:]}


def test_guide_follows_model_status(pack):
    g = rows(an.judging_guide(pack))
    assert g["수익성"][0] == "dupont(계산 안 됨), ohlson(점수 있음)"
    assert g["수익성"][1] == "OH-1, OH-2, OH-3" and g["수익성"][2] == "지지, 반박, 엇갈림"
    assert g["성장"] == ["없음", "없음", "근거없음"]
    assert "<judging_guide>" in an.build_messages(pack)[1]["content"]


def test_guide_says_hold_when_every_checked_model_is_unscored(pack):
    held = {**pack, "inputs": {**pack["inputs"], "models": [
        {**m, "status": "held"} if m["model"] == "ohlson" else m for m in pack["inputs"]["models"]]}}
    assert rows(an.judging_guide(held))["수익성"][1:] == ["없음", "판단보류"]


def test_verdict_is_written_after_reason_and_stances(pack):
    props = list(an.assessment_schema(pack)["properties"]["claims"]["items"]["properties"])
    assert props.index("claim_stance") < props.index("reason") < props.index("evidence_stance") < props.index("verdict")


@pytest.mark.parametrize("claim, evidence, expected", [
    ("좋음", "좋음", ["지지"]), ("나쁨", "나쁨", ["지지"]), ("좋음", "나쁨", ["반박"]), ("나쁨", "좋음", ["반박"]),
    ("좋음", "엇갈림", ["엇갈림"]), ("나쁨", "근거 없음", ["판단보류", "근거없음"]),
])
def test_expected_verdicts(claim, evidence, expected):
    assert an.expected_verdicts(claim, evidence) == expected


def answer(**kw):
    claim = {"id": "C1", "text": "원자재 가격이 올라 영업이익률이 떨어진다", "type": "수익성",
             "models_to_check": ["dupont", "ohlson"], "claim_stance": "나쁨", "evidence_ids": ["OH-2"],
             "reason": "RI Spread가 올라 이익이 좋아졌다(OH-2).", "evidence_stance": "좋음", "verdict": "반박",
             "proposition_ids": [], "graph_note": ""}
    claim.update(kw)
    return {"claims": [claim], "injection_detected": False, "summary": "수익성 주장은 Ohlson 근거로 반박된다.",
            "data_limits": ["DuPont은 계산되지 않음"]}


def errors(issues):
    return [i for i in issues if i.startswith("오류")]


def test_falling_margin_claim_with_improving_evidence_must_be_refuted(pack):
    assert errors(an.validate_assessment(answer(), pack)) == []
    issues = errors(an.validate_assessment(answer(verdict="지지"), pack))
    assert any("주장 방향 '나쁨', 근거 방향 '좋음'와 판정 '지지'이 맞지 않음" in i for i in issues)
    assert not any("반박" in i for i in issues)          # 어느 쪽이 틀렸는지 모르므로 판정을 정해 주지 않는다


def test_no_evidence_while_ohlson_scored_is_an_error(pack):
    issues = errors(an.validate_assessment(answer(verdict="근거없음", evidence_ids=[], evidence_stance="근거 없음"), pack))
    assert any("'근거없음'인데 확인할 모델 ['ohlson']가 점수를 냈음" in i for i in issues)


def test_old_answers_without_stances_are_still_checked(pack):
    old = answer()
    for k in ("claim_stance", "evidence_stance"):
        del old["claims"][0][k]
    assert errors(an.validate_assessment(old, pack)) == []


@pytest.fixture(scope="module")
def alarm_pack():
    return bp.build_pack("053660", "2016-06-30", "2019-06-28", "주가가 큰 출렁임 없이 꾸준히 오를 것이다.", include_dupont=False)


def gate_answer(**kw):
    claim = {"id": "C1", "text": "주가가 큰 출렁임 없이 꾸준히 오를 것이다.", "type": "주가흐름", "models_to_check": ["gate"],
             "claim_stance": "좋음", "evidence_ids": ["GT-1", "GT-2"], "reason": "경보가 1번 켜졌고 누적 수익률은 올랐다(GT-1, GT-2).",
             "evidence_stance": "엇갈림", "verdict": "엇갈림", "proposition_ids": [], "graph_note": ""}
    claim.update(kw)
    return {"claims": [claim], "injection_detected": False, "summary": "주가 흐름은 엇갈린다.", "data_limits": []}


def test_alarm_in_hold_cannot_be_read_as_good(alarm_pack):
    assert an.gate_alarm_in_hold(alarm_pack)
    assert "evidence_stance는 나쁨 또는 엇갈림" in rows(an.judging_guide(alarm_pack))["주가흐름"][2]
    assert errors(an.validate_assessment(gate_answer(), alarm_pack)) == []
    issues = errors(an.validate_assessment(gate_answer(evidence_stance="좋음", verdict="지지"), alarm_pack))
    assert any("보유 중 가격 경보가 켜졌는데" in i for i in issues)
