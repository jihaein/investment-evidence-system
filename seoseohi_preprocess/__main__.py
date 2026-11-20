import argparse
from . import preprocess

def main():
    parser=argparse.ArgumentParser(description='DART/KRX 원본을 새 버전의 CSV 6개로 변환')
    parser.add_argument('raw_dir')
    parser.add_argument('out_dir')
    parser.add_argument('--strict',action='store_true',help='종목 하나라도 입력 오류면 전체 실패(기본: 그 종목만 격리)')
    args=parser.parse_args()
    preprocess(args.raw_dir,args.out_dir,strict=args.strict)
    print('완료:',args.out_dir)

if __name__=='__main__':main()
