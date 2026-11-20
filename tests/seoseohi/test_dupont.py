import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from models.seoseohi.dupont import load_ttm, company_pm_at_date

def test_pm_known_value():
    load_dotenv()
    source = os.getenv("SEOSEOHI_TTM_DIR")
    if not source or not (Path(source) / "ttm_final.csv").is_file():
        pytest.skip("DuPont reference fixture is not configured")
    ttm = load_ttm()
    r = company_pm_at_date(ttm, "036890", "2019-12-30")
    assert round(r["pm"], 4) == 8.4987
