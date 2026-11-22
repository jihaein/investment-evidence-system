"""Immutable six-file releases for the PREPROCESS_HANDOFF interface.

All money arithmetic uses Decimal. Unresolvable cells stay NULL with evidence
requirements. out_dir must be a NEW version directory; publish its path only
after this function returns. See README for the input evidence sidecar.
"""
from __future__ import annotations
import calendar
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
import zipfile

from .ltd import engine as LTD, dartdoc as LTD_DOC

VERSION = '3.0.0'
SCHEMA = json.loads(Path(__file__).with_name('schema.json').read_text(encoding='utf-8'))
ALIASES = json.loads(Path(__file__).with_name('aliases.json').read_text(encoding='utf-8'))
FILES = {name.split('_')[0]: name for name in SCHEMA}
Q = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}
# R-M1: daily_market.py (kosdaq120_2020_2026) writes REPORTED_POSITIVE_MARKETCAP for MKTCAP>0.
# ZERO_MARKETCAP_REPORTED_REVIEW / INVALID_MARKETCAP_REVIEW stay NULL + review.
MARKET_OK = {'OK', 'VERIFIED', 'VALID', 'REPORTED_POSITIVE_MARKETCAP'}
DASHES = {'-', '–', '—', '－'}
NUMERIC = {'BIGINT', 'INTEGER', 'DOUBLE', 'NUMERIC'}
BALANCE = {'ifrs_Assets', 'ifrs_CurrentAssets', 'ifrs_Liabilities', 'ifrs_CurrentLiabilities',
           'ifrs_Equity', 'ifrs_CashAndCashEquivalents', 'dart_LongTermBorrowingsGross',
           'ifrs_EquityAttributableToOwnersOfParent', 'ifrs_NoncontrollingInterests', 'ifrs_IssuedCapital'}


class PreprocessError(ValueError):
    """Input or release contract violation; existing releases are untouched."""


def iso(value):
    s = str(value).strip()
    if re.fullmatch(r'\d{8}', s):
        s = f'{s[:4]}-{s[4:6]}-{s[6:]}'
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        raise PreprocessError(f'INVALID_DATE: {s}')
    return date.fromisoformat(s).isoformat()


def code(value, length):
    if not isinstance(value, str) or not re.fullmatch(r'\d{' + str(length) + r'}', value):
        raise PreprocessError(f'INVALID_CODE: expected {length}-digit string')
    return value


def amount(value, *, dash_zero=False):
    if value is None or str(value).strip() == '':
        return None
    s = unicodedata.normalize('NFKC', str(value)).strip().replace('−', '-').replace('△', '-')
    if s in {'-', '–', '—'}:
        return Decimal(0) if dash_zero else None
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', s):
        raise PreprocessError('INVALID_AMOUNT')
    return Decimal(s.replace(',', ''))


def txt(value):
    return '' if value is None else format(value, 'f') if isinstance(value, Decimal) else str(value)


def read_csv(path, *, two=False):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        header = next(reader)
        if len(set(header)) != len(header):
            raise PreprocessError('DUPLICATE_HEADER')
        if two:
            next(reader)
        result = []
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise PreprocessError('CSV_WIDTH_MISMATCH')
            result.append(dict(zip(header, row)))
        return result


def write_csv(path, rows, header, labels=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(header)
        if labels is not None:
            w.writerow(labels)
        for row in rows:
            w.writerow([txt(row.get(c)) for c in header])


@dataclass(frozen=True)
class Fact:
    value: Decimal | None
    receipts: tuple[str, ...] = ()
    method: str = 'SOURCE_MISSING'
    evidence: str = ''


def calc(method, *facts, op):
    receipts = tuple(sorted({r for f in facts for r in f.receipts}))
    if any(f.value is None for f in facts):
        return Fact(None, receipts, 'OPERAND_MISSING:' + method)
    with localcontext() as ctx:
        ctx.prec = 50
        return Fact(op(*[f.value for f in facts]), receipts, method,
                    ' | '.join(f.evidence for f in facts if f.evidence))


def ratio(a, b, multiplier=1):
    if b.value == 0:
        return Fact(None, tuple(sorted(set(a.receipts + b.receipts))), 'ZERO_DENOMINATOR')
    return calc('RATIO', a, b, op=lambda x, y: x / y * multiplier)


class Engine:
    def __init__(self, root, strict=False, *, metadata=None, dependency_as_of=None):
        self.strict = strict
        self.dependency_as_of = dependency_as_of
        self.root = Path(root)
        self.raw = self.root / '_raw_pull'
        self.out = {k: [] for k in FILES}
        self.lineage = []
        self.review = []
        self.sources = {}
        self.master = read_csv(self.root / FILES['12'], two=True)
        self.companies = {}
        stocks = set()
        for r in self.master:
            corp, stock = code(r['corp_code'], 8), code(r['stock_code'], 6)
            if corp in self.companies or stock in stocks:
                raise PreprocessError('DUPLICATE_MASTER')
            self.companies[corp] = r
            stocks.add(stock)
        if not self.master:
            raise PreprocessError('EMPTY_MASTER')
        self.metadata = (self.load_optional(self.raw / 'preprocess_metadata.json', {})
                         if metadata is None else metadata)
        self.filings = self.metadata.get('filings', {})
        for rec, row in self.filings.items():
            code(rec, 14)
            iso(row['disclosed_date'])
            iso(row['period_end'])
            if iso(row['period_end']) > iso(row['disclosed_date']):
                raise PreprocessError('PERIOD_END_AFTER_DISCLOSURE')
            if not isinstance(row.get('fiscal_year'), int):
                raise PreprocessError('FISCAL_YEAR_REQUIRED')
        days = self.metadata.get('trading_days', [])
        self.days = sorted(set(iso(d) for d in days))
        self.override_map = {}
        # 1) 확인값 대장(_raw_pull/verified_ledger/*.jsonl): 납품본에서 옮긴 접수번호별 확인값.
        #    이번 입력에 없는 접수번호의 줄은 건너뜁니다(대장은 전체 이력 저장소).
        self.ledger_stats = {'loaded': 0, 'skipped_receipt_not_in_input': 0, 'used': 0}
        for path in sorted((self.raw / 'verified_ledger').glob('*.jsonl')):
            data = path.read_bytes()
            self.sources[str(path.relative_to(self.root))] = hashlib.sha256(data).hexdigest()
            for n, line in enumerate(data.decode('utf-8-sig').splitlines(), 1):
                if not line.strip():
                    continue
                x = json.loads(line)
                self.check_fact(x, f'{path.name}:{n}')
                k = (x['rcept_no'], x['fs_div'], x['metric'], x['slot'])
                if k in self.override_map:
                    raise PreprocessError('DUPLICATE_LEDGER_FACT: ' + '/'.join(k))
                if x['rcept_no'] not in self.filings:
                    self.ledger_stats['skipped_receipt_not_in_input'] += 1
                    continue
                x = dict(x, _origin='ledger:' + path.name)
                self.override_map[k] = x
                self.ledger_stats['loaded'] += 1
        # 2) metadata.verified_facts: 이번 실행용 명시 확인값. 대장보다 우선하며 접수번호 누락은 오류.
        self.overrides = self.metadata.get('verified_facts', [])
        seen = set()
        for x in self.overrides:
            self.check_fact(x, 'metadata')
            k = (x['rcept_no'], x['fs_div'], x['metric'], x['slot'])
            if k in seen:
                raise PreprocessError('INVALID_VERIFIED_FACT')
            seen.add(k)
            if x['rcept_no'] not in self.filings:
                raise PreprocessError('VERIFIED_FACT_RECEIPT_UNLISTED')
            self.override_map[k] = dict(x, _origin='metadata')
        self.quarantined = {}
        self.ltd_docs = self.raw / 'dart/document'
        self.reports = []
        self.market = {}

    @staticmethod
    def check_fact(x, where):
        for key in ('rcept_no', 'fs_div', 'metric', 'slot'):
            if not isinstance(x.get(key), str) or not x[key]:
                raise PreprocessError('INVALID_VERIFIED_FACT: ' + where)
        if not x.get('evidence') or not x.get('approved_by'):
            raise PreprocessError('INVALID_VERIFIED_FACT: ' + where)
        if x.get('value') is None and not x.get('reason'):
            raise PreprocessError('VERIFIED_NULL_REASON_REQUIRED: ' + where)

    def load_optional(self, path, default):
        if not path.exists():
            return default
        data = path.read_bytes()
        self.sources[str(path.relative_to(self.root))] = hashlib.sha256(data).hexdigest()
        return json.loads(data.decode('utf-8-sig'))

    def quarantine(self, corp, error):
        """한 종목의 입력 오류는 그 종목만 이번 릴리스에서 빼고 검토 목록에 남깁니다(strict=True 이면 전체 실패)."""
        if self.strict:
            raise error
        if corp not in self.quarantined:
            self.quarantined[corp] = str(error)
            self.issue('', 'corp_code', 'COMPANY_QUARANTINED', f'{corp}: {error}')

    def issue(self, receipt, column, reason, detail=''):
        self.review.append({'rcept_no': receipt, 'column': column, 'reason': reason, 'detail': detail})

    def load(self):
        # Historical snapshots may be stored below fnlttSinglAcntAll/archive.
        seen = {}
        for path in sorted((self.raw / 'dart/fnlttSinglAcntAll').rglob('*.json')):
            match = re.fullmatch(r'(\d{8})_(\d{4})_(11013|11012|11014|11011)_(CFS|OFS)', path.stem)
            if not match:
                raise PreprocessError('INVALID_FINANCIAL_FILENAME: ' + path.name)
            corp, year, report, basis = match.groups()
            if corp not in self.companies:
                self.issue('', '', 'OUTSIDE_UNIVERSE', corp)
                continue
            try:
                self.load_file(path, corp, year, report, basis, seen)
            except PreprocessError as e:
                self.quarantine(corp, e)
        self.finish_load()

    def load_file(self, path, corp, year, report, basis, seen):
        payload = self.load_optional(path, {})
        if payload.get('status') == '013':
            return
        if payload.get('status') != '000' or not payload.get('list'):
            raise PreprocessError('DART_RESPONSE_INVALID: ' + path.name)
        accounts = payload['list']
        receipts = {code(a.get('rcept_no'), 14) for a in accounts}
        if len(receipts) != 1:
            raise PreprocessError('MIXED_RECEIPTS')
        rec = receipts.pop()
        for a in accounts:
            if any(str(a.get(k)) != v for k, v in [('corp_code', corp), ('bsns_year', year), ('reprt_code', report)]):
                raise PreprocessError('ACCOUNT_CONTEXT_MISMATCH')
        meta = self.filings.get(rec)
        if not meta:
            raise PreprocessError('FILINGS_METADATA_REQUIRED: ' + rec + ' (공시일을 접수번호로 추정하지 않습니다)')
        if meta.get('corp_code') != corp or meta.get('reprt_code') != report or str(meta.get('bsns_year')) != year:
            raise PreprocessError('FILING_CONTEXT_MISMATCH: ' + rec)
        canonical = json.dumps(accounts, sort_keys=True, ensure_ascii=False)
        key = (rec, basis)
        if key in seen:
            if seen[key] != canonical:
                raise PreprocessError('SAME_RECEIPT_CONFLICT: ' + rec)
            return
        seen[key] = canonical
        self.reports.append({'corp': corp, 'year': int(year), 'fy': meta['fiscal_year'],
                             'q': Q[report], 'report': report, 'basis': basis,
                             'rec': rec, 'date': iso(meta['disclosed_date']),
                             'end': iso(meta['period_end']), 'accounts': accounts})

    def finish_load(self, *, read_market=True):
        if not self.reports:
            raise PreprocessError('NO_FINANCIAL_INPUT' + (': all companies quarantined ' + json.dumps(
                self.quarantined, ensure_ascii=False)[:500] if self.quarantined else ''))
        # CFS must be present, or absence explicitly confirmed. A missing file
        # alone cannot distinguish HTTP failure from a missing statement.
        available = {(r['rec'], r['basis']) for r in self.reports}
        selected = []
        for r in self.reports:
            if r['basis'] == 'OFS':
                if (r['rec'], 'CFS') in available:
                    continue
                if self.filings[r['rec']].get('cfs_absent_verified') is not True:
                    self.quarantine(r['corp'], PreprocessError('CFS_ABSENCE_UNVERIFIED: ' + r['rec']))
            selected.append(r)
        present = {r['rec'] for r in selected}
        for rec in self.filings:
            if self.filings[rec].get('required', False) and rec not in present:
                corp = self.filings[rec].get('corp_code')
                err = PreprocessError('REQUIRED_HISTORICAL_VERSION_MISSING: ' + rec)
                if corp not in self.companies:
                    raise err
                self.quarantine(corp, err)
        self.reports = sorted((r for r in selected if r['corp'] not in self.quarantined),
                              key=lambda r: (r['date'], r['rec']))
        if not self.reports:
            raise PreprocessError('NO_FINANCIAL_INPUT: all companies quarantined ' + json.dumps(self.quarantined, ensure_ascii=False)[:500])
        # Current and historical market rows must agree; never last-file-wins.
        if not read_market:
            return
        market_dir = self.root / '추가자료/시장자료'
        paths = list(market_dir.glob('120종목_일별시가총액_*.csv'))
        paths.extend(market_dir.glob('krx_session_*.csv'))
        if (market_dir / 'krx_market.csv').exists():
            paths.append(market_dir / 'krx_market.csv')
        for p in sorted(paths):
            self.sources[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            for row in read_csv(p, two=p.name in self.metadata.get('two_header_market_files', [])):
                stock = code(row.get('stock_code'), 6)
                day = iso(row['BAS_DD'])
                key = (stock, day)
                value = (txt(amount(row.get('MKTCAP'))), row.get('quality_status', ''))
                if key in self.market and self.market[key] != value:
                    raise PreprocessError('MARKET_VALUE_CONFLICT: ' + str(key))
                self.market[key] = value

    def fact(self, r, metric, slot='thstrm_amount'):
        override = self.override_map.get((r['rec'], r['basis'], metric, slot))
        if override:
            receipts = tuple(override.get('operand_receipts', [r['rec']]))
            for rec in receipts:
                if rec not in self.filings or iso(self.filings[rec]['disclosed_date']) > r['date']:
                    raise PreprocessError('FUTURE_OR_UNKNOWN_OVERRIDE_SOURCE')
            value = amount(override.get('value'))
            self.__dict__.setdefault('_used_keys', set()).add((r['rec'], r['basis'], metric, slot))
            self.ledger_stats['used'] = len(self._used_keys)
            if override.get('approx'):
                self.issue(r['rec'], metric, 'VERIFIED_VALUE_IS_APPROX_OR_ESTIMATE', override.get('method', ''))
            # Scoped verified values are never transplanted to a new receipt.
            return Fact(value, receipts, ('VERIFIED:' + override.get('method', 'OVERRIDE')) if value is not None
                        else override['reason'], override['evidence'])
        statement = {'BS'} if metric in BALANCE or metric == 'SUM' else {'CF'} if metric == 'ifrs_CashFlowsFromUsedInOperatingActivities' else {'IS', 'CIS'}
        rows = [a for a in r['accounts'] if a.get('sj_div') in statement and a.get('account_detail', '') in ('', '-', '연결재무제표', '별도재무제표')]
        candidates = [a for a in rows if a.get('account_id', '').replace('ifrs-full_', 'ifrs_') == metric]
        if not candidates:
            names = {re.sub(r'\s+', '', n) for n in ALIASES.get(metric, [])}
            candidates = [a for a in rows if re.sub(r'\s+', '', a.get('account_nm', '')) in names]
        if not candidates:
            return Fact(None, (), 'ACCOUNT_NOT_FOUND')
        values = set()
        dash = False
        for a in candidates:
            if a.get('currency') != 'KRW':
                return Fact(None, (r['rec'],), 'CURRENCY_UNVERIFIED')
            raw = unicodedata.normalize('NFKC', str(a.get(slot) or '')).strip()
            if raw in DASHES:
                dash = True
            try:
                # R-P21: a '-' printed in the statement amount cell means 0 KRW.
                value = amount(a.get(slot), dash_zero=True)
            except PreprocessError:
                return Fact(None, (r['rec'],), 'INVALID_AMOUNT')
            if value is not None and value != value.to_integral_value():
                return Fact(None, (r['rec'],), 'NONINTEGER_KRW')
            values.add(value)
        if len(values) != 1:
            return Fact(None, (r['rec'],), 'AMBIGUOUS_ACCOUNT')
        value = values.pop()
        method = 'API_REPORTED_DASH_ZERO' if dash and value == 0 else 'API_REPORTED' if value is not None else 'SOURCE_BLANK'
        return Fact(value, (r['rec'],), method,
                    ';'.join(a.get('account_id', '') + ':' + a.get('account_nm', '') + ':' + slot for a in candidates))

    def cell(self, r, column, compute):
        """Final output cell: a verified ledger value for this receipt wins; otherwise compute()."""
        if (r['rec'], r['basis'], column, 'output_cell') in self.override_map:
            return self.fact(r, column, 'output_cell')
        return compute()

    def owner(self, r, metric, slot='thstrm_amount'):
        direct = self.fact(r, metric, slot)
        if direct.method.startswith('VERIFIED:') or (r['rec'], r['basis'], metric, slot) in self.override_map:
            return direct
        if direct.value is not None and 'Equity' in metric:
            # R-O5: owner equity line carrying the share-capital amount (우주일렉트로 등) -> NULL + review.
            cap = self.fact(r, 'ifrs_IssuedCapital', slot)
            total = self.fact(r, 'ifrs_Equity', slot)
            if cap.value is not None and cap.value == direct.value and total.value is not None and total.value != direct.value:
                self.issue(r['rec'], metric, 'OWNER_EQUITY_EQUALS_ISSUED_CAPITAL_REVIEW', str(direct.value))
                return Fact(None, direct.receipts, 'OWNER_EQUITY_EQUALS_ISSUED_CAPITAL_REVIEW', direct.evidence)
        if direct.value is not None or direct.method != 'ACCOUNT_NOT_FOUND':
            return direct
        equity = 'Equity' in metric
        total = self.fact(r, 'ifrs_Equity' if equity else 'ifrs_ProfitLoss', slot)
        nci = self.fact(r, 'ifrs_NoncontrollingInterests' if equity else 'ifrs_ProfitLossAttributableToNoncontrollingInterests', slot)
        if nci.value is not None:
            return calc('TOTAL_MINUS_EXPLICIT_NCI', total, nci, op=lambda a, b: a-b)
        if r['basis'] == 'OFS':
            return Fact(total.value, total.receipts, 'OFS_TOTAL_EQUALS_OWNER', total.evidence)
        if nci.method != 'ACCOUNT_NOT_FOUND':
            # R-O3: NCI line exists but blank/ambiguous -> NULL, never assume 0.
            return Fact(None, total.receipts, 'NCI_LINE_PRESENT_VALUE_MISSING')
        # R-O2 / R-P10 (PM 2026-11-04): no NCI line on BS and no NCI share in P&L
        # -> total equity / profit is the owners' amount, flagged.
        other = self.fact(r, 'ifrs_ProfitLossAttributableToNoncontrollingInterests' if equity else 'ifrs_NoncontrollingInterests')
        if other.method != 'ACCOUNT_NOT_FOUND' and other.value != 0:
            return Fact(None, total.receipts, 'OWNER_SPLIT_UNVERIFIED')
        if total.value is None:
            return Fact(None, total.receipts, 'OPERAND_MISSING:TOTAL_EQUALS_OWNERS_NO_NCI_SPLIT')
        self.issue(r['rec'], metric, 'TOTAL_EQUALS_OWNERS_NO_NCI_SPLIT', 'R-O2/R-P10 승인 규칙 적용(표시)')
        return Fact(total.value, total.receipts, 'TOTAL_EQUALS_OWNERS_NO_NCI_SPLIT', total.evidence)

    def prior(self, r, fy, q, basis=None):
        options = [x for x in self.reports if x['corp'] == r['corp'] and x['basis'] == (basis or r['basis'])
                   and x['fy'] == fy and x['q'] == q
                   and x['date'] <= (self.dependency_as_of or r['date'])]
        return max(options, key=lambda x: (x['date'], x['rec'])) if options else None

    def get(self, r, metric, slot='thstrm_amount'):
        return self.owner(r, metric, slot) if 'AttributableToOwnersOfParent' in metric else self.fact(r, metric, slot)

    def ytd(self, r, metric):
        slot = 'thstrm_amount' if r['q'] in (1, 4) or metric == 'ifrs_CashFlowsFromUsedInOperatingActivities' else 'thstrm_add_amount'
        return self.get(r, metric, slot)

    def quarter(self, r, metric):
        if r['q'] != 4 and metric != 'ifrs_CashFlowsFromUsedInOperatingActivities':
            return self.get(r, metric)
        if r['q'] == 1:
            return self.get(r, metric)
        prev = self.prior(r, r['fy'], r['q']-1)
        if not prev and r['q'] == 4 and r['basis'] == 'CFS':
            # R-F2 exception: Q3 report had no CFS (verified absent) -> Q3 OFS YTD, flagged.
            prev = self.prior(r, r['fy'], 3, basis='OFS')
            if prev:
                f = calc('YTD_DIFFERENCE', self.ytd(r, metric), self.ytd(prev, metric), op=lambda a, b: a-b)
                self.issue(r['rec'], metric, 'Q3_YTD_NO_CFS_STATEMENT_OFS_USED', prev['rec'])
                return Fact(f.value, f.receipts, f.method + '|Q3_YTD_NO_CFS_STATEMENT_OFS_USED', f.evidence)
        if not prev:
            return Fact(None, (), 'PRIOR_SAME_BASIS_YTD_MISSING')
        return calc('YTD_DIFFERENCE', self.ytd(r, metric), self.ytd(prev, metric), op=lambda a, b: a-b)

    def ttm(self, r, metric):
        if r['q'] == 4:
            return self.get(r, metric)
        annual = self.prior(r, r['fy']-1, 4)
        if not annual:
            return Fact(None, (), 'PRIOR_SAME_BASIS_ANNUAL_MISSING')
        # DART frmtrm_amount for an interim IS is not previous-year YTD.
        prior_ytd = self.get(r, metric, 'frmtrm_q_amount' if r['q'] == 1 else 'frmtrm_add_amount')
        return calc('CURRENT_YTD_PLUS_PRIOR_FY_MINUS_COMPARATIVE_YTD', self.ytd(r, metric),
                    self.get(annual, metric), prior_ytd, op=lambda a, b, c: a+b-c)

    def put(self, row, column, fact, file, receipt):
        if fact.value is not None and fact.value < 0 and any(s in column for s in
                ('Assets', 'CashAndCashEquivalents', 'CurrentLiabilities', 'LongTermBorrowingsGross')):
            fact = Fact(None, fact.receipts, 'NEGATIVE_BALANCE_REVIEW', fact.evidence)
        row[column] = txt(fact.value)
        self.lineage.append({'file': FILES[file], 'rcept_no': receipt, 'column': column,
                             'fiscal_year': row.get('fiscal_year', ''), 'value': txt(fact.value),
                             'method': fact.method, 'operand_receipts': '|'.join(fact.receipts),
                             'evidence': fact.evidence})
        if fact.value is None:
            self.issue(receipt, column, fact.method)
        elif abs(fact.value) > Decimal('1e14'):
            self.issue(receipt, column, 'LARGE_AMOUNT_REVIEW')

    def build_financial(self):
        for r in self.reports:
            if r['corp'] in self.quarantined:
                continue
            try:
                self.build_report(r)
            except PreprocessError as e:
                self.quarantine(r['corp'], e)
        if self.quarantined:
            # 격리된 종목은 이미 만든 행까지 이번 릴리스에서 모두 뺍니다(부분 출력 금지).
            bad = set(self.quarantined)
            recs = {r['rec'] for r in self.reports if r['corp'] in bad}
            for k in ('02', '04', '11', '11B'):
                self.out[k] = [x for x in self.out[k] if x.get('corp_code') not in bad]
            self.lineage = [x for x in self.lineage if x['rcept_no'] not in recs]
            self.review = [x for x in self.review if x['rcept_no'] not in recs]
            if not self.out['04']:
                raise PreprocessError('NO_FINANCIAL_OUTPUT: all companies quarantined')

    def build_report(self, r):
        m = self.companies[r['corp']]
        rec = r['rec']
        base = dict(stock_code=m['stock_code'], corp_code=r['corp'], stock_name_2014=m.get('stock_name', m.get('corp_name', '')),
                    fs_div=r['basis'], bsns_year=r['fy'], quarter=r['q'], rcept_no=rec)
        # Historical stock names may be supplied by the fixed universe master.
        if m.get('stock_name_2014'):
            base['stock_name_2014'] = m['stock_name_2014']
        next_day = next((d for d in self.days if d > r['date']), '')
        if not next_day:
            self.issue(rec, 'available_from', 'TRADING_CALENDAR_MISSING_OR_NOT_EXTENDED')
        available = next_day.replace('-', '')
        dates = dict(disclosed_date=r['date'], available_from=available, period_end=r['end'])
        market, quality = self.market.get((m['stock_code'], next_day), ('', 'MISSING'))
        if market and amount(market) < 0:
            quality = 'NEGATIVE_MARKET_CAP'
        if quality not in MARKET_OK:
            market = ''
            self.issue(rec, 'MKTCAP', 'MARKET_MISSING_OR_UNVERIFIED', quality)
        bas_dd = available
        pinned = self.override_map.get((rec, r['basis'], 'MKTCAP', 'output_cell'))
        if pinned:
            # PM 개별 결정: 시가총액 기준일을 이용가능일과 다르게 지정(예: 222040 2025 사업보고서, PM 11-19).
            # 대장에 bas_dd 와 그날의 값이 함께 있어야 하며, 시세 파일 값과 다르면 실패합니다.
            day = iso(pinned['bas_dd'])
            seen = self.market.get((m['stock_code'], day), ('', 'MISSING'))
            if seen[0] != txt(amount(pinned['value'])) or seen[1] not in MARKET_OK or day <= r['date']:
                raise PreprocessError('PINNED_MARKET_VALUE_MISMATCH: ' + rec)
            market, bas_dd = seen[0], day.replace('-', '')
            self.issue(rec, 'MKTCAP', 'PM_PINNED_MARKET_DATE', f"BAS_DD {bas_dd} (available_from {available}): {pinned['evidence']}")
        oh = {**base, **dates, 'reprt_code': r['report'], 'report_period': {1:'1분기보고서',2:'반기보고서',3:'3분기보고서',4:'사업보고서'}[r['q']],
              'BAS_DD': bas_dd, 'MKTCAP': market}
        owner_equity = self.cell(r, 'ifrs_EquityAttributableToOwnersOfParent_thstrm_amount',
                                 lambda: self.owner(r, 'ifrs_EquityAttributableToOwnersOfParent'))
        owner_profit = self.cell(r, 'ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount',
                                 lambda: self.quarter(r, 'ifrs_ProfitLossAttributableToOwnersOfParent'))
        # BS comparative at an interim report is prior FY end, not t-4q.
        prior_equity = self.cell(r, 'ifrs_EquityAttributableToOwnersOfParent_prior_fy_end',
                                 lambda: self.owner(r, 'ifrs_EquityAttributableToOwnersOfParent', 'frmtrm_amount'))
        values = {'ifrs_EquityAttributableToOwnersOfParent_thstrm_amount': owner_equity,
                  'ifrs_EquityAttributableToOwnersOfParent_prior_fy_end': prior_equity,
                  'ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount': owner_profit,
                  'reported_total_equity_thstrm_amount': self.fact(r, 'ifrs_Equity'),
                  'reported_profitloss_quarter_amount': self.quarter(r, 'ifrs_ProfitLoss')}
        for c, f in values.items():
            self.put(oh, c, f, '04', rec)
        oh['owner_value_source'] = owner_equity.method + '|' + owner_profit.method
        oh['prior_basis_note'] = prior_equity.method
        oh['quarter_amount_method'] = owner_profit.method
        oh['quarter_amount_formula'] = 'FY-Q3_YTD' if r['q'] == 4 else 'REPORTED_3_MONTH'
        oh['q3_ytd_rcept_no'] = '|'.join(x for x in owner_profit.receipts if x != rec) if r['q'] == 4 else ''
        if 'Q3_YTD_NO_CFS_STATEMENT_OFS_USED' in owner_profit.method:
            oh['basis_fallback_reason'] = 'Q3_YTD_NO_CFS_STATEMENT_OFS_USED'
        oh['ttm_status'] = 'COMPLETE' if all(oh.get(c) for c in list(values)[:3] + ['MKTCAP']) else 'MISSING'
        self.out['04'].append(oh)
        du = dict(base)
        revenue = self.cell(r, 'ifrs_Revenue_ttm', lambda: self.ttm(r, 'ifrs_Revenue'))
        operating = self.cell(r, 'dart_OperatingIncomeLoss_ttm', lambda: self.ttm(r, 'dart_OperatingIncomeLoss'))
        assets = self.cell(r, 'ifrs_Assets_t', lambda: self.fact(r, 'ifrs_Assets'))
        previous = self.prior(r, r['fy']-1, r['q'])
        old_assets = self.fact(previous, 'ifrs_Assets') if previous else Fact(None, (), 'PRIOR_SAME_QUARTER_ASSETS_MISSING')
        if previous and r['q'] != 4:
            # Require a true one-year matching period end (not an FY-end balance).
            expected = date.fromisoformat(r['end'])
            last_year_day = min(expected.day, calendar.monthrange(expected.year-1, expected.month)[1])
            if previous['end'] != expected.replace(year=expected.year-1, day=last_year_day).isoformat():
                old_assets = Fact(None, (), 'PERIOD_END_MISMATCH')
        if r['q'] == 4:
            # A_t-4 at Q4 = prior fiscal year end. Use this annual report's own comparative
            # (same basis, restatement included; PM 2026-11-19, matches delivered 120종목 data).
            # Interim reports' BS comparative is prior FY end, NOT the prior same quarter, so it is never used for Q1~Q3.
            comparative = self.fact(r, 'ifrs_Assets', 'frmtrm_amount')
            if comparative.value is not None:
                old_assets = Fact(comparative.value, comparative.receipts,
                                  'SAME_REPORT_PRIOR_FY_END_COMPARATIVE', comparative.evidence)
        old_assets = self.cell(r, 'ifrs_Assets_t_4q', lambda: old_assets)
        average = calc('AVERAGE_ASSETS', assets, old_assets, op=lambda a,b:(a+b)/2)
        facts = {'ifrs_Revenue_q': self.cell(r, 'ifrs_Revenue_q', lambda: self.quarter(r, 'ifrs_Revenue')),
                 'dart_OperatingIncomeLoss_q': self.cell(r, 'dart_OperatingIncomeLoss_q',
                                                         lambda: self.quarter(r, 'dart_OperatingIncomeLoss')),
                 'ifrs_Revenue_ttm': revenue, 'dart_OperatingIncomeLoss_ttm': operating,
                 'ifrs_Assets_t': assets, 'ifrs_Assets_t_4q': old_assets,
                 'PM_pct': ratio(operating, revenue, 100), 'ATO': ratio(revenue, average)}
        for c,f in facts.items():
            self.put(du, c, f, '11', rec)
        periods = [(r['fy']*4+r['q']-1-i) for i in range(3,-1,-1)]
        du['ttm_quarters'] = ','.join(f'{n//4}Q{n%4+1}' for n in periods)
        self.out['11'].append(du)
        self.out['11B'].append({**base, **dates, 'period_end_4q': previous['end'] if previous else '', 'ttm_quarters': du['ttm_quarters']})
        if r['q'] == 4:
            self.annual(r, base)

    def ltd(self, r, slot):
        """R-F: 장기부채 합계(유동성 장기차입금·유동성 사채·장기차입금·사채)를 공시 원문에서 대사.

        2026-11-12 납품 때 쓴 엔진(work/kq120_2026_fill/engine4.py)을 그대로 사용합니다.
        원문(document.zip)이 없으면 NULL. 대사 규칙이 성립하지 않으면 NULL + 사유.
        """
        per = {'thstrm_amount': 'cur', 'frmtrm_amount': 'prior'}.get(slot)
        if per is None:
            return Fact(None, (), 'LTD_NOT_DERIVED_FOR_' + slot.upper())
        # 원화가 아닌 재무제표(중국 기업 CNY 등)는 원문 숫자를 원으로 볼 수 없으므로 계산하지 않습니다.
        currencies = {a.get('currency') for a in r['accounts']}
        if currencies != {'KRW'}:
            return Fact(None, (r['rec'],), 'LTD_CURRENCY_UNVERIFIED', ','.join(sorted(str(c) for c in currencies)))
        z = self.ltd_docs / f"{r['rec']}.zip"
        if not z.exists():
            z = self.ltd_docs / r['rec'] / 'document.zip'
        if not z.exists():
            return Fact(None, (), 'LTD_DOCUMENT_NOT_PULLED')
        cache = self.__dict__.setdefault('_ltd_cache', {})
        key = (r['rec'], r['basis'])
        if key not in cache:
            self.sources[str(z.relative_to(self.root))] = hashlib.sha256(z.read_bytes()).hexdigest()
            totals = [self.fact(r, m).value for m in ('ifrs_Liabilities', 'ifrs_Assets', 'ifrs_CurrentLiabilities', 'ifrs_Equity')]
            try:
                docs = LTD_DOC.load(str(z))
            except (zipfile.BadZipFile, OSError) as e:
                cache[key] = {'status': 'DOC_CORRUPT', 'err': str(e)}
            else:
                cache[key] = LTD.analyze(r['rec'], r['basis'], docs, [float(t) for t in totals if t is not None])
        res = cache[key]
        if res.get('status') != 'OK':
            return Fact(None, (r['rec'],), 'LTD_ENGINE_' + str(res.get('status')))
        p = res['per'].get(per, {})
        if p.get('rule') is None or p.get('value') is None:
            return Fact(None, (r['rec'],), 'LTD_ENGINE_UNRESOLVED', str(p.get('why', ''))[:300])
        value = Decimal(str(p['value'])).quantize(Decimal(1))
        flags = '|'.join(p.get('flags', []))
        method = 'LTD_ENGINE:' + p['rule'] + (('|' + flags) if flags else '')
        evidence = f"{res.get('bs_doc')} 표{res.get('bs_table')} 단위 {res.get('bs_unit')}; " + \
                   '; '.join(f'{lab} {v:,.0f} {how}' for lab, v, how in p.get('parts', []))[:600]
        candidate = f'{slot} 후보값 {value} ({method})'
        # 천원 단위 근사 분리(D_NOTE_SPLIT_천원근사)는 실공시 대조 정확도 95%라 자동 반영하지 않습니다.
        if '천원근사' in flags:
            self.issue(r['rec'], 'SUM', 'LTD_APPROX_CANDIDATE_NOT_PUBLISHED', candidate)
            return Fact(None, (r['rec'],), 'LTD_APPROX_CANDIDATE_REVIEW', evidence)
        # 단위 오류 방지: 부채총계보다 크거나, 부채가 10억 이상인데 100만원 미만이면 단위 의심.
        liabilities = self.fact(r, 'ifrs_Liabilities', slot).value
        if liabilities is not None and (value > liabilities or (0 < value < 1_000_000 and liabilities >= 1_000_000_000)):
            self.issue(r['rec'], 'SUM', 'LTD_UNIT_SUSPECT_NOT_PUBLISHED', candidate + f' / 부채총계 {liabilities}')
            return Fact(None, (r['rec'],), 'LTD_UNIT_SUSPECT_REVIEW', evidence)
        if self.metadata.get('ltd_auto_publish', 'exact') == 'none':
            self.issue(r['rec'], 'SUM', 'LTD_CANDIDATE_AWAITING_APPROVAL', candidate)
            return Fact(None, (r['rec'],), 'LTD_CANDIDATE_AWAITING_APPROVAL', evidence)
        if 'P_PM_NONCURRENT_ONLY' in flags:
            self.issue(r['rec'], 'SUM', 'LTD_PM_NONCURRENT_ONLY_RULE_APPLIED', slot)
        self.issue(r['rec'], 'SUM', 'LTD_ENGINE_VALUE_SAMPLE_REVIEW', f'{slot} {method}')
        return Fact(value, (r['rec'],), method, evidence)

    def annual(self, r, base):
        metrics = [c['name'] for c in SCHEMA[FILES['02']]['columns'] if c['data_type'] == 'BIGINT']
        for offset, slot in enumerate(['thstrm_amount', 'frmtrm_amount', 'bfefrmtrm_amount']):
            row = {**base, 'fiscal_year': r['fy']-offset, 'amount_column': slot, 'disclosed_date': r['date']}
            for metric in metrics:
                f = self.fact(r, metric, slot)
                if metric == 'SUM' and (r['rec'], r['basis'], 'SUM', slot) not in self.override_map:
                    f = self.ltd(r, slot)
                if metric == 'ifrs_CostOfSales' and f.method in ('ACCOUNT_NOT_FOUND', 'SOURCE_BLANK'):
                    f = calc('SAME_REPORT_REVENUE_MINUS_GROSS_PROFIT', self.fact(r, 'ifrs_Revenue', slot),
                             self.fact(r, 'ifrs_GrossProfit', slot), op=lambda a,b:a-b)
                    if f.value is None:
                        # Rulebook FDS COGS: revenue - SG&A - operating income, flagged '역산'.
                        sga = self.fact(r, 'dart_TotalSellingGeneralAdministrativeExpenses', slot)
                        if sga.value is not None:
                            f = calc('DERIVED_REVENUE_MINUS_SGA_MINUS_OI(역산)', self.fact(r, 'ifrs_Revenue', slot), sga,
                                     self.fact(r, 'dart_OperatingIncomeLoss', slot), op=lambda a,b,c:a-b-c)
                            if f.value is not None:
                                self.issue(r['rec'], metric, 'DERIVED_COGS_REVIEW', '역산 표시')
                        if f.value is None:
                            # 영업수익·영업비용 양식: NULL with reason code.
                            f = Fact(None, f.receipts, 'NOT_APPLICABLE_NO_COST_OF_SALES')
                self.put(row, metric, f, '02', r['rec'])
            sum_method = next((x['method'] for x in reversed(self.lineage)
                               if x['column'] == 'SUM' and x['rcept_no'] == r['rec']), '')
            if row.get('SUM') and row.get('dart_LongTermBorrowingsGross') and not sum_method.startswith('VERIFIED:'):
                # 확인값(대장)은 PM 승인 범위 차이(예: 묶인 유동 차입금 제외)가 있을 수 있어 덮지 않습니다.
                if amount(row['SUM']) < amount(row['dart_LongTermBorrowingsGross']):
                    self.issue(r['rec'], 'SUM', 'LTD_BELOW_COMPONENT_REVIEW')
                    row['SUM'] = ''
                    self.lineage[-1].update(value='', method='LTD_BELOW_COMPONENT_REVIEW')
            self.out['02'].append(row)

    def build_other(self):
        for corp, m in self.companies.items():
            p = self.raw / f'dart/company/{corp}.json'
            payload = self.load_optional(p, {})
            if payload and (payload.get('status') != '000' or payload.get('stock_code') != m['stock_code']):
                # 마스터 행은 유지하고 API 기업개황만 쓰지 않습니다.
                if self.strict:
                    raise PreprocessError('COMPANY_RESPONSE_INVALID')
                self.issue('', 'corp_code', 'COMPANY_RESPONSE_INVALID_MASTER_ROW_KEPT', corp)
                payload = {}
            row = {**m, **{k:v for k,v in payload.items() if k in {c['name'] for c in SCHEMA[FILES['12']]['columns']}}}
            row['corp_code'] = corp
            row['in_sample'] = m.get('in_sample') or 'N'
            row['ksic_mid_2digit'] = row.get('induty_code','')[:2]
            self.out['12'].append(row)
        for p in sorted((self.raw / 'dart/irdsSttus').rglob('*.json')):
            payload = self.load_optional(p, {})
            if payload.get('status') == '013':
                continue
            if payload.get('status') != '000':
                if self.strict:
                    raise PreprocessError('ISSUANCE_RESPONSE_INVALID')
                self.issue('', '', 'ISSUANCE_RESPONSE_INVALID_FILE_SKIPPED', p.name)
                continue
            for a in payload.get('list', []):
                if a.get('corp_code') not in self.companies or a.get('corp_code') in self.quarantined:
                    continue
                row = dict(a)
                if row.get('stock_code') != self.companies[row['corp_code']]['stock_code']:
                    if self.strict:
                        raise PreprocessError('ISSUANCE_STOCK_MISMATCH')
                    self.issue(row.get('rcept_no', ''), 'stock_code', 'ISSUANCE_STOCK_MISMATCH_ROW_SKIPPED', p.name)
                    continue
                for c in SCHEMA[FILES['09']]['columns']:
                    key = c['name']
                    if c['data_type'] in NUMERIC:
                        row[key] = txt(amount(row.get(key)))
                        if row[key] == '':
                            self.issue(row.get('rcept_no',''), key, 'SOURCE_BLANK_OR_NOT_APPLICABLE')
                    elif c['data_type'] == 'DATE':
                        raw = row.get(key, '')
                        try:
                            row[key] = iso(raw) if raw not in ('', '-') else ''
                        except (ValueError, PreprocessError):
                            row[key] = ''
                            self.issue(row.get('rcept_no',''), key, 'INVALID_SOURCE_DATE', str(raw))
                self.out['09'].append(row)


def validate_output(folder):
    """Offline equivalent of packaged YAML structural checks, plus joins.

    This is not a claim that the Soda executable has been run.
    """
    folder = Path(folder)
    tables = {}
    for name, schema in SCHEMA.items():
        with (folder/name).open(encoding='utf-8-sig',newline='') as f:
            rr=csv.reader(f)
            if next(rr) != [c['name'] for c in schema['columns']] or next(rr) != schema['labels']:
                raise PreprocessError('SCHEMA_OR_LABEL_MISMATCH: '+name)
        rows = read_csv(folder/name, two=True)
        for row in rows:
            for col in schema['columns']:
                value = row[col['name']]
                for check in col.get('checks', []):
                    if 'missing' in check and not value:
                        raise PreprocessError('REQUIRED_VALUE_MISSING: '+col['name'])
                    invalid = check.get('invalid', {})
                    if value and 'valid_length' in invalid:
                        code(value, invalid['valid_length'])
                    if value and 'valid_values' in invalid and value not in invalid['valid_values']:
                        raise PreprocessError('ENUM_INVALID')
                if value and col['data_type'] in NUMERIC:
                    v=amount(value)
                    if v is None or (col['data_type'] in {'INTEGER','BIGINT'} and v != v.to_integral_value()):
                        raise PreprocessError('NUMERIC_TYPE_INVALID')
                if value and col['data_type']=='DATE':
                    if iso(value)!=value:raise PreprocessError('DATE_FORMAT_INVALID')
            for k in ('BAS_DD','available_from'):
                if row.get(k):
                    if not re.fullmatch(r'\d{8}',row[k]):raise PreprocessError('COMPACT_DATE_REQUIRED')
                    if row.get('disclosed_date') and iso(row[k])<=iso(row['disclosed_date']):
                        raise PreprocessError('AVAILABILITY_NOT_AFTER_DISCLOSURE')
        for check in schema['checks']:
            if 'duplicate' in check:
                keys=check['duplicate']['columns']
                seen=set()
                for row in rows:
                    k=tuple(row[c] for c in keys)
                    if k in seen:raise PreprocessError('DUPLICATE_KEY: '+name)
                    seen.add(k)
        tables[name.split('_')[0]]=rows
    receipt_sets=[{r['rcept_no'] for r in tables[k]} for k in ('04','11','11B')]
    if not receipt_sets[0]==receipt_sets[1]==receipt_sets[2]:
        raise PreprocessError('CROSS_FILE_RECEIPT_MISMATCH')
    return {FILES[k]:len(v) for k,v in tables.items()}


def preprocess(raw_dir: str, out_dir: str, *, strict: bool = False) -> None:
    """Read a RAWDATA_DIR root and create a NEW immutable six-CSV release.

    Original handoff inputs plus _raw_pull/preprocess_metadata.json are used.
    Metadata dates are mandatory; missing cell-level evidence yields NULL.
    Existing out_dir is rejected. Caller switches its RAWDATA_DIR after return.
    """
    root, target = Path(raw_dir).resolve(), Path(out_dir).resolve()
    if target.exists() or target == root or root in target.parents:
        raise PreprocessError('OUTPUT_MUST_BE_NEW_DIRECTORY_OUTSIDE_INPUT')
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ('.'+target.name+'.lock')
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise PreprocessError('RELEASE_ALREADY_RUNNING') from None
    os.close(handle)
    stage = Path(tempfile.mkdtemp(prefix='.'+target.name+'-', dir=target.parent))
    try:
        engine = Engine(root, strict=strict)
        engine.sources[FILES['12']] = hashlib.sha256((root/FILES['12']).read_bytes()).hexdigest()
        engine.load()
        engine.build_financial()
        engine.build_other()
        for prefix, rows in engine.out.items():
            s=SCHEMA[FILES[prefix]]
            write_csv(stage/FILES[prefix], rows, [c['name'] for c in s['columns']], s['labels'])
        counts=validate_output(stage)
        audit=stage/'추가자료'
        write_csv(audit/'cell_lineage.csv',engine.lineage,['file','rcept_no','fiscal_year','column','value','method','operand_receipts','evidence'])
        write_csv(audit/'review_queue.csv',engine.review,['rcept_no','column','reason','detail'])
        manifest={'package_version':VERSION,'contract_version':VERSION,'row_counts':counts,
                  'review_count':len(engine.review), 'input_sha256':engine.sources,
                  'output_sha256':{name:hashlib.sha256((stage/name).read_bytes()).hexdigest() for name in SCHEMA},
                  'validation':'LOCAL_SCHEMA_AND_CROSS_FILE_CHECKS_PASSED',
                  'soda_executed':False,'historical_manual_parity_verified':False,
                  'quarantined_companies':engine.quarantined,
                  'verified_ledger':engine.ledger_stats,
                  'strict':strict}
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        # Refuse changes made by a concurrently running pull.
        for rel,digest in engine.sources.items():
            if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=digest:
                raise PreprocessError('INPUT_CHANGED_DURING_BUILD')
        os.rename(stage,target)
    finally:
        if stage.exists():shutil.rmtree(stage)
        lock.unlink(missing_ok=True)
