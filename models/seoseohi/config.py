DUPONT_PM_ADJ_CUTS = [-1.0, -3.0, -6.0] # %p, 초기가설


def score_pm(dPM_adj_pp) -> int | None: 
    """
    PM 점수 계산
    :param dPM_adj_pp: PM_adj_pp 값
    :return: PM 점수
    """
    if dPM_adj_pp is None:
        return None
    count = sum(1 for c in DUPONT_PM_ADJ_CUTS if dPM_adj_pp <= c)
    return count

def _test() -> int | None:
    print(score_pm(2) == 0) # 0 
    print(score_pm(-0.5) == 0) # 0
    print(score_pm(-2) == 1) # 1
    print(score_pm(-4) == 2) # 2 
    print(score_pm(-7) == 3) # 3
    print(score_pm(-14.44) == 3) # 3

_test()