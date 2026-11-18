"""
archive_snapshots.py — 원천 스냅샷(모델 입력 파일) 트리를 로컬 zip 하나로 묶는다.
이 zip 을 Supabase Storage(또는 R2)에 올리면 Google Drive 공유폴더 의존을 끊을 수 있다.

실행:  .venv/bin/python data_pipeline/archive_snapshots.py
결과:  data_pipeline/_archive/seoseohi_rawdata_2020-2026.zip  (원본 207MB → 압축 ~100MB 예상, CSV 라 잘 줄어듦)

다음 단계(수동, 대시보드가 제일 간단):
  Supabase → Storage → New bucket(예: "rawdata", private) → 이 zip 업로드.
  (업로드 후 코드의 BASE26 경로를 이 아카이브 기준으로 바꾸는 작업은 아침에 함께.)

주의: 만들어진 zip 은 git 에 커밋하지 말 것(용량 큼). _archive/ 는 .gitignore 에 추가 권장.
"""
import time
import zipfile
from pathlib import Path

SRC = Path(
    "/Users/jihaein/Library/CloudStorage/GoogleDrive-j.in26mcgill@gmail.com/"
    ".shortcut-targets-by-id/1RS50TgL31WxhExC3ZVXca6bMTj-c9yCU/skku_fds_main/"
    "데이터/서서히 모델 백테스트/개발자전달_본데이터_120종목_백테스트_2020-2026"
)
OUT_DIR = Path("/Users/jihaein/Developer/investment-evidence-system/data_pipeline/_archive")
ZIP = OUT_DIR / "seoseohi_rawdata_2020-2026.zip"


def main():
    if not SRC.exists():
        raise SystemExit(f"❌ 원천 폴더를 찾을 수 없음(구글드라이브 경로 확인):\n{SRC}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = [p for p in SRC.rglob("*") if p.is_file()]
    print(f"{len(files)}개 파일 압축 시작 → {ZIP.name}")
    t0 = time.time()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for i, p in enumerate(files, 1):
            z.write(p, p.relative_to(SRC))     # 폴더 구조 유지해서 압축
            if i % 50 == 0:
                print(f"  {i}/{len(files)}", end="\r")
    mb = ZIP.stat().st_size / 1e6
    print(f"\n✓ 완료 — {ZIP}")
    print(f"  크기 {mb:.0f} MB · 소요 {time.time()-t0:.0f}s")
    print("  다음: Supabase 대시보드 → Storage → 버킷 만들고 이 zip 업로드")


if __name__ == "__main__":
    main()
