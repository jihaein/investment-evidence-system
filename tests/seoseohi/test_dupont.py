from models.seoseohi.dupont import load_ttm, company_pm_at_date

def test_pm_known_value():
    ttm = load_ttm()
    r = company_pm_at_date(ttm, "036890", "2019-12-30")
    assert round(r["pm"], 4) == 8.4987
