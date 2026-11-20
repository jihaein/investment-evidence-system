# DART 전체재무제표 1건만 받아 응답 '모양'을 확인하는 연습용 스크립트
import os, sys, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
KEY = os.environ["OPENDART_API_KEY"]

corp_code  = sys.argv[1] if len(sys.argv) > 1 else "00526696"  # 웨이브일렉트로닉스
bsns_year  = "2024"
reprt_code = "11011"   # 11011=사업보고서(연간) · 11013=1분기 · 11012=반기 · 11014=3분기

url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
params = {
    "crtfc_key":  KEY,
    "corp_code":  corp_code,
    "bsns_year":  bsns_year,
    "reprt_code": reprt_code,
    "fs_div":     "CFS",        # CFS=연결재무제표, OFS=별도
}

data = requests.get(url, params=params, timeout=20).json()

hint = {"000":"정상", "010":"키 미등록", "011":"키 사용제한", "013":"조회 데이터 없음",
        "020":"일일 사용한도 초과", "100":"파라미터 오류"}
print("status:", data.get("status"), "→", hint.get(data.get("status"), data.get("message")))

rows = data.get("list", [])
print("받은 계정 수:", len(rows))
for a in rows[:8]:                               # 앞 8개만 미리보기
    print(f"  [{a['sj_div']}] {a['account_nm']:<16} 당기={a['thstrm_amount']}")