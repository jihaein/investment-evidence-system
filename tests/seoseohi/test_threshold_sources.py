"""임계 모델 주가 원천 바꾸기(use_prices): KRX 형식(종목 거르기)과 팀 120종목 시장자료 형식, 기본으로 되돌리기."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("threshold_src_test", ROOT / "models" / "seoseohi" / "threshold.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)


def _krx(path):
    path.write_text("BAS_DD,ISU_CD,ISU_NM,TDD_CLSPRC,FLUC_RT,ACC_TRDVOL\n기준일,코드,이름,종가,등락률,거래량\n"
                    "20200102,005930,삼성전자,100,1.0,10\n20200103,005930,삼성전자,98,-2.0,0\n"
                    "20200102,000660,SK하이닉스,50,0.5,5\n", encoding="utf-8-sig")


def test_krx_format_with_code_filter(tmp_path):
    f = tmp_path / "prices.csv"
    _krx(f)
    p = T.load_prices(f, codes={"005930"})
    assert list(p["stock_code"].unique()) == ["005930"] and list(p["ret_krx"]) == [0.01, -0.02]
    assert list(p["volume"]) == [10, 0]                       # 거래량 0 = 거래정지로 쓰인다


def test_team_market_format_uses_close_change(tmp_path):
    f = tmp_path / "grid.csv"
    f.write_text("stock_code,stock_name_2014,BAS_DD,MKT_NM,MKTCAP,LIST_SHRS,TDD_CLSPRC,quality_status\n"
                 "5930,삼성전자,20200102,KOSPI,1,1,100,ok\n5930,삼성전자,20200103,KOSPI,1,1,110,ok\n"
                 "660,SK하이닉스,20200102,KOSPI,1,1,50,ok\n", encoding="utf-8-sig")
    p = T.load_prices(f)
    a = p[p["stock_code"] == "005930"]
    assert np.isnan(a["ret_krx"].iloc[0]) and a["ret_krx"].iloc[1] == pytest.approx(0.10)
    assert set(p["company_name"]) == {"삼성전자", "SK하이닉스"} and (p["volume"] > 0).all()


def test_use_prices_switches_source_and_back(tmp_path):
    f = tmp_path / "prices.csv"
    _krx(f)
    try:
        T.use_prices(f, codes=["5930"])
        assert set(T.prepared_prices()) == {"005930"}
    finally:
        T.use_prices()
    assert T._SOURCE == {"path": T.PRICE_PATH, "codes": None}
