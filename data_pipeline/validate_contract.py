"""
validate_contract.py — preprocess 산출물(6개 CSV)이 데이터컨트랙트에 맞는지 검사.
load 전에 돌려, 규격 위반이면 '시끄럽게' 실패시킨다(나쁜 데이터가 DB로 들어가기 전에 차단).

검사(엄격): 컬럼 이름·순서 · 키 컬럼 결측 · 코드 길이 · 허용값(fs_div 등) · 키 중복.
행 수(row_count must_be): 고정 본데이터 기준이라 라이브 데이터에선 달라짐 → 참고(info)만.

정본 규격 = data/rawdata/데이터컨트랙트_본데이터.yaml (이 파일이 기준. 여기 코드는 그걸 읽어 검사만 함)
실행:  .venv/bin/python data_pipeline/validate_contract.py          # 대상 = RAWDATA_DIR
       .venv/bin/python data_pipeline/validate_contract.py --dir <폴더>
필요:  .venv/bin/python -m pip install pyyaml
"""
import os
import sys
import argparse
import pandas as pd
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")
try:
    import yaml
except ImportError:
    sys.exit("PyYAML 필요 →  .venv/bin/python -m pip install pyyaml")

CONTRACT = f"{ROOT}/data/rawdata/데이터컨트랙트_본데이터.yaml"


def load_specs(path):
    """YAML 멀티문서 → dataset 가진 문서들만."""
    with open(path, encoding="utf-8-sig") as f:
        return [d for d in yaml.safe_load_all(f) if d and "dataset" in d]


def check_file(path, spec):
    """한 파일을 계약과 대조. (errors, infos) 반환."""
    errs, infos = [], []
    df = pd.read_csv(path, skiprows=[1], dtype=str, keep_default_na=False)  # 2줄 헤더(2행=한글설명) 건너뜀
    cols = list(df.columns)
    want = [c["name"] for c in spec["columns"]]

    # 1) 스키마: 컬럼 이름·순서 고정
    if cols != want:
        missing = [c for c in want if c not in cols]
        extra   = [c for c in cols if c not in want]
        if missing: errs.append(f"컬럼 누락: {missing}")
        if extra:   errs.append(f"없어야 할 컬럼: {extra}")
        if not missing and not extra: errs.append("컬럼 순서가 계약과 다름")

    # 2) 상위 checks: row_count(참고) · duplicate(엄격)
    for chk in spec.get("checks", []) or []:
        if "row_count" in chk:
            must = (chk["row_count"] or {}).get("threshold", {}).get("must_be")
            if must is not None and len(df) != must:
                infos.append(f"행 수 {len(df):,} (고정본 기준 {must:,}) — 라이브 데이터면 정상")
        if "duplicate" in chk:
            keys = chk["duplicate"]["columns"]
            if all(k in df.columns for k in keys):
                dups = int(df.duplicated(subset=keys).sum())
                if dups: errs.append(f"키 중복 {dups}건: {keys}")

    # 3) 컬럼별 checks: missing · invalid(valid_length / valid_values)
    for c in spec["columns"]:
        col = c["name"]
        if col not in df.columns:
            continue
        s = df[col]
        blank = s.str.strip() == ""
        for chk in (c.get("checks") or []):
            if "missing" in chk:
                n = int(blank.sum())
                if n: errs.append(f"{col}: 필수인데 빈칸 {n}건")
            if "invalid" in chk:
                inv = chk["invalid"] or {}
                nb = s[~blank]
                if "valid_length" in inv:
                    L = inv["valid_length"]
                    bad = int((nb.str.len() != L).sum())
                    if bad: errs.append(f"{col}: 길이 {L} 아님 {bad}건")
                if "valid_values" in inv:
                    allowed = set(inv["valid_values"])
                    bad = int((~nb.isin(allowed)).sum())
                    if bad: errs.append(f"{col}: 허용값 {sorted(allowed)} 밖 {bad}건")
    return errs, infos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=f"{ROOT}/data/rawdata", help="검사할 CSV 폴더(preprocess 산출물)")
    args = ap.parse_args()

    specs = load_specs(CONTRACT)
    print(f"계약 파일 {len(specs)}개 검사 — 대상 폴더: {args.dir}\n")
    total_err = 0
    for spec in specs:
        fname = os.path.basename(spec["dataset"])
        path = os.path.join(args.dir, fname)
        if not os.path.exists(path):
            print(f"✗ {fname}: 파일 없음"); total_err += 1; continue
        errs, infos = check_file(path, spec)
        print(f"{'✓' if not errs else '✗'} {fname}")
        for m in infos: print(f"    · {m}")
        for e in errs:  print(f"    ✗ {e}")
        total_err += len(errs)

    if total_err:
        print(f"\n계약 위반 {total_err}건 — 파이프라인 중단(나쁜 데이터가 DB로 들어가는 것을 막음)")
        sys.exit(1)
    print("\n✓ 모든 계약 검사 통과 — load 진행 가능")


if __name__ == "__main__":
    main()
