"""
RAWDATA_DIR 의 원본 스냅샷 폴더를 zip 하나로 묶는다 (Supabase Storage 백업용).
실행: .venv/bin/python data_pipeline/archive_snapshots.py
결과: data_pipeline/_archive/seoseohi_rawdata_2020-2026.zip (git 에는 올리지 않음)
"""
import os
import time
import zipfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")

SRC = Path(os.environ["RAWDATA_DIR"])
OUT_DIR = Path("/Users/jihaein/Developer/investment-evidence-system/data_pipeline/_archive")
ZIP = OUT_DIR / "seoseohi_rawdata_2020-2026.zip"


def main():
    if not SRC.exists():
        raise SystemExit(f"원천 폴더를 찾을 수 없음: {SRC}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = [p for p in SRC.rglob("*") if p.is_file()]
    print(f"{len(files)}개 파일 압축 시작 → {ZIP.name}")
    t0 = time.time()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for i, p in enumerate(files, 1):
            z.write(p, p.relative_to(SRC))
            if i % 50 == 0:
                print(f"  {i}/{len(files)}", end="\r")
    mb = ZIP.stat().st_size / 1e6
    print(f"\n완료: {ZIP}")
    print(f"  크기 {mb:.0f} MB · 소요 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
