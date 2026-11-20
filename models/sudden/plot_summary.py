"""plot_summary.py — 파이널 시험(120종목 2020~2026) 결과 그림을 docs/img/에 만든다.

읽는 것 (모두 이미 계산된 결과, 다시 계산하지 않는다):
  cache/final_trial_20261120/final_report.json      (report_final_trial.py가 만든 스트레스 지표)
  cache/naver_120_redirect_recovered_20261120/ach/summary.json   (판정 분포)
  cache/stress_120_final/montecarlo_*.json           (워크포워드)

    .venv\\Scripts\\python.exe models\\sudden\\plot_summary.py
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / 'docs/img'
REPORT = HERE / 'cache/final_trial_20261120/final_report.json'
SUMMARY = HERE / 'cache/naver_120_redirect_recovered_20261120/ach/summary.json'
MC_DIR = HERE / 'cache/stress_120_final'

# 기본 팔레트(검증된 범주 순서 1~5), 글자는 잉크 색, 격자는 흐리게
SURFACE, INK, INK2, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df'
SERIES = {'무손절': '#2a78d6', '단순손절': '#eb6834', '임계': '#1baf7a', '급락': '#eda100', '임계_그리고_급락': '#e87ba4'}
POS, NEG, NEUTRAL = '#2a78d6', '#e34948', '#b9b8b2'
HOLD_REASON = {'no_evidence_in_window': '사건 창 근거 없음', 'market_rule_only': '시장 규칙만',
               'weak_support': '근거 약함', 'competing_groups_too_close': '두 묶음 팽팽',
               'no_supported_hypothesis': '지지 가설 없음', 'collection_incomplete': '수집 미완료'}


def style():
    plt.rcParams.update({'font.family': 'Malgun Gothic', 'axes.unicode_minus': False, 'font.size': 10,
                         'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE, 'savefig.facecolor': SURFACE,
                         'axes.edgecolor': GRID, 'axes.labelcolor': INK2, 'xtick.color': INK2, 'ytick.color': INK2,
                         'text.color': INK, 'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.8,
                         'axes.spines.top': False, 'axes.spines.right': False, 'legend.frameon': False})


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(OUT / name)


def stress_returns(metrics):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    years = [1, 2, 3, 4]
    for name, color in SERIES.items():
        ys = [100 * next(r['return_mean'] for r in metrics if r['strategy'] == name and r['horizon'] == h) for h in years]
        lw = 2.6 if name == '급락' else 2
        ax.plot(years, ys, color=color, lw=lw, marker='o', ms=6, label=name, zorder=3 if name == '급락' else 2)
        if name == '급락':                                 # 선끝 이름표는 강조 선 하나만(나머지는 범례)
            ax.annotate(f'{name} {ys[-1]:+.1f}%', (4, ys[-1]), xytext=(8, 0), textcoords='offset points',
                        va='center', fontsize=9, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xticks(years, [f'{y}년' for y in years])
    ax.set_xlim(0.8, 4.9)
    ax.set_ylabel('평균 누적수익률 (%)')
    ax.set_title('보유 기간별 평균 누적수익률 (150런 평균, 다음 거래일 매도)', loc='left', fontsize=11)
    ax.legend(loc='lower left', fontsize=9, ncol=2)
    save(fig, 'sudden_stress_returns.png')


def stress_winrates(metrics):
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8), sharey=True)
    years = [1, 2, 3, 4]
    names = ['급락', '임계', '임계_그리고_급락']
    for ax, key, title in [(axes[0], 'win_hold', '무손절보다 수익이 높은 런 비율'),
                           (axes[1], 'win_random', '같은 수 무작위 매도보다 높은 런 비율')]:
        for name in names:
            ys = [next(r[key] for r in metrics if r['strategy'] == name and r['horizon'] == h) for h in years]
            ax.plot(years, ys, color=SERIES[name], lw=2.6 if name == '급락' else 2, marker='o', ms=6, label=name)
        ax.axhline(0.5, color=INK2, lw=1, ls='--')
        ax.text(4.2, 0.5, '0.5\n우연', va='center', fontsize=8, color=INK2)
        ax.set_xlim(0.8, 4.55)
        ax.set_xticks(years, [f'{y}년' for y in years])
        ax.set_ylim(0, 1)
        ax.set_title(title, loc='left', fontsize=10)
    axes[0].set_ylabel('승률')
    axes[0].legend(loc='lower left', fontsize=9)
    save(fig, 'sudden_stress_winrates.png')


def verdicts(summary):
    v, h = summary['verdicts'], summary['hold_reasons']
    rows = [('가치훼손 (매도 신호)', v['가치훼손'], POS), ('비가치', v['비가치'], NEUTRAL)]
    rows += [(f'보류: {HOLD_REASON.get(k, k)}', n, '#d9d8d3') for k, n in sorted(h.items(), key=lambda x: -x[1])]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    labels, vals, colors = zip(*rows)
    y = range(len(rows))[::-1]
    ax.barh(list(y), vals, color=colors, height=0.7)
    for yi, n in zip(y, vals):
        ax.text(n + 6, yi, f'{n}', va='center', fontsize=9, color=INK)
    ax.set_yticks(list(y), labels)
    ax.grid(axis='y', visible=False)
    ax.set_xlabel('사건 수')
    total = sum(v.values())
    ax.set_title(f'판정 분포 (급락 사건 {total:,}건, 판단보류 {v["판단보류"]}건은 사유별)', loc='left', fontsize=11)
    save(fig, 'sudden_verdicts.png')


def walkforward():
    files = list(MC_DIR.glob('montecarlo_*.json'))
    assert len(files) == 1
    tables = json.loads(files[0].read_text(encoding='utf-8'))['tables']
    rows = []
    for scenario, records in tables.items():
        if '워크포워드' not in scenario:
            continue
        get = {r['strategy']: r for r in records}
        rows.append((scenario.split(' ')[0], 100 * (get['급락']['누적수익률'] - get['무손절']['누적수익률']),
                     get['급락']['win_vs_무손절']))
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    xs = [r[0] for r in rows]
    diffs = [r[1] for r in rows]
    ax.bar(xs, diffs, color=[POS if d >= 0 else NEG for d in diffs], width=0.6)
    for x, d, w in rows:
        ax.text(x, d + (0.8 if d >= 0 else -0.8), f'{d:+.1f}%p\n승률 {w:.1f}', ha='center',
                va='bottom' if d >= 0 else 'top', fontsize=8.5, color=INK)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_ylim(min(diffs) - 8, max(diffs) + 6)
    ax.grid(axis='x', visible=False)
    ax.set_xlabel('진입 연도 (보유 2년, 바스켓 10개)')
    ax.set_ylabel('급락 - 무손절 (%p)')
    ax.set_title('워크포워드: 진입 연도별 급락 매도의 초과 수익', loc='left', fontsize=11)
    save(fig, 'sudden_walkforward.png')


def main():
    style()
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    metrics = report['stress']['전체']
    stress_returns(metrics)
    stress_winrates(metrics)
    verdicts(json.loads(SUMMARY.read_text(encoding='utf-8')))
    walkforward()


if __name__ == '__main__':
    main()
