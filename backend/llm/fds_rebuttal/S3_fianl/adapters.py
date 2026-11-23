"""Latest CSV adapter and point-in-time observations for 120 companies."""
import calendar
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

KST = timezone(timedelta(hours=9))
FORMULAS = {
    "divide": (2, lambda x, y: x / y),
    "gross_margin": (2, lambda x, y: (x - y) / x),
}


def digest(value):
    packed = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                        separators=(",", ":")).encode()
    return hashlib.sha256(packed).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cutoff_for(as_of):
    return datetime.combine(as_of - timedelta(days=1), time(23, 59, 59), KST).isoformat()


def rows_of(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("empty or duplicate CSV header")
        for line, row in enumerate(reader, 2):
            if None in row or None in row.values():
                raise ValueError(f"CSV width mismatch at line {line}")
            yield line, row


def parse_period(label):
    if not re.fullmatch(r"[0-9]{4}Q[1-4]", label):
        raise ValueError("invalid_calendar_quarter")
    year, quarter = int(label[:4]), int(label[-1])
    month = 3 * quarter
    return Period("quarterly", f"{year}-{month-2:02d}-01",
                  date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
                  year * 4 + quarter - 1)


def published_on(value, receipt, period):
    if not value:
        raise ValueError("unknown_publication_date")
    day = date.fromisoformat(value)
    if not re.fullmatch(r"[0-9]{14}", receipt) or receipt[:8] != day.strftime("%Y%m%d"):
        raise ValueError("receipt_date_mismatch")
    if day.isoformat() < period.end:
        raise ValueError("published_before_period_end")
    return datetime.combine(day, time(23, 59, 59), KST).isoformat()


def number(raw, spec):
    if raw == "":
        raise ValueError("empty_value")
    try:
        value = Decimal(raw) * Decimal(str(spec.get("scale", 1)))
    except InvalidOperation:
        raise ValueError("nonnumeric_value") from None
    if not value.is_finite() or not math.isfinite(float(value)):
        raise ValueError("nonfinite_value")
    if ("min" in spec and value < Decimal(str(spec["min"]))) or (
            "max" in spec and value > Decimal(str(spec["max"]))):
        raise ValueError("outside_valid_range")
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass(frozen=True)
class Period:
    frequency: str
    start: str
    end: str
    ordinal: int


@dataclass(frozen=True)
class Point:
    company_id: str
    corp_code: str
    node_id: str
    period: Period
    value: int | float
    unit: str
    measure: str
    basis: str
    available_at: str
    source: str
    priority: int
    evidence: dict
    input_fact_ids: tuple = ()
    fact_id: str = field(init=False)

    def __post_init__(self):
        signature = dict(vars(self), period=asdict(self.period))
        object.__setattr__(self, "fact_id", "S3-" + digest(signature)[:32])

    @property
    def slot(self):
        return self.basis, self.period

    def record(self):
        return dict(asdict(self), period_end=self.period.end)


def load_companies(path, spec):
    companies, corp_codes = {}, set()
    for line, row in rows_of(path):
        stock, corp, name = row[spec["company_field"]], row[spec["corp_field"]], row[spec["name_field"]]
        if (not re.fullmatch(r"[0-9]{6}", stock) or not re.fullmatch(r"[0-9]{8}", corp)
                or not name or (stock in companies and companies[stock] !=
                                {"corp_code": corp, "name": name, "fiscal_month": row[spec["fiscal_month_field"]]})
                or (stock not in companies and corp in corp_codes)):
            raise ValueError(f"invalid/duplicate company at line {line}")
        companies[stock] = {"corp_code": corp, "name": name, "fiscal_month": row[spec["fiscal_month_field"]]}
        corp_codes.add(corp)
    return companies


def formula_order(formulas, nodes):
    by_id = {f["node_id"]: f for f in formulas}
    if len(by_id) != len(formulas):
        raise ValueError("duplicate formula target")
    ordered, active, done = [], set(), set()

    def visit(node):
        if node in active:
            raise ValueError(f"cyclic formula: {node}")
        if node in done or node not in by_id:
            return
        active.add(node)
        f = by_id[node]
        if node not in nodes or f["function"] not in FORMULAS or len(f["inputs"]) != FORMULAS[f["function"]][0]:
            raise ValueError(f"unregistered formula: {node}")
        for input_id in f["inputs"]:
            if input_id not in nodes:
                raise ValueError(f"unknown formula input: {input_id}")
            visit(input_id)
        active.remove(node)
        done.add(node)
        ordered.append(f)

    for node in by_id:
        visit(node)
    return ordered


class Inputs:
    def __init__(self, root, config, nodes):
        self.config = config
        self.raw = defaultdict(list)
        self.q4 = defaultdict(list)  # registry reader compatibility; no derived Q4 in this source
        self.audit = {}
        self.rejections = []
        self.overrides = {}
        self.units = defaultdict(set)
        self.confirmed = set()
        self.formulas = formula_order(config["formulas"], nodes)
        sources = config["sources"]
        if len(sources) != 1:
            raise ValueError("latest CSV configuration expects one source")
        source, spec = next(iter(sources.items()))
        path = root / spec["file"]
        self.companies = load_companies(path, spec)
        if set(self.companies) != {r[spec["company_field"]] for _, r in rows_of(path)}:
            raise ValueError("company roster mismatch")
        for mapping in [*spec["mappings"], *self.formulas]:
            node = mapping["node_id"]
            if node not in nodes or (not nodes[node]["observable"] and not mapping.get("override")):
                raise ValueError(f"unknown node or missing override rationale: {node}")
            self.units[node].add(mapping["unit"])
            if mapping.get("override"):
                self.overrides[node] = mapping["override"]
        if any(len(units) != 1 for units in self.units.values()):
            raise ValueError("incompatible canonical units")
        self.read_source(source, spec, path)
        for company in self.companies:
            resolved, _, _ = self.resolve(company, date.max)
            self.confirmed.update(node for node, series in resolved.items() if series)

    def read_source(self, source, spec, path):
        all_rows = list(rows_of(path))
        periods = Counter(row[spec["period_field"]] for _, row in all_rows)
        if set(periods.values()) != {len(self.companies)}:
            raise ValueError("each quarter must contain exactly one row per company")
        if len({(row[spec["company_field"]], row[spec["period_field"]]) for _, row in all_rows}) != len(all_rows):
            raise ValueError("duplicate company/quarter row")
        self.audit[source] = {"file": spec["file"], "exists": True, "rows": len(all_rows),
                              "periods": dict(sorted(periods.items())), "sha256": file_hash(path),
                              "valid_fields": 0, "no_filing_rows": 0}
        stat = self.audit[source]
        for line, row in all_rows:
            stock = row[spec["company_field"]]
            company = self.companies[stock]
            if row[spec["corp_field"]] != company["corp_code"] or row[spec["name_field"]] != company["name"]:
                raise ValueError(f"company identity changed at line {line}")
            period = parse_period(row[spec["period_field"]])
            if row[spec["status_field"]] != "공시":
                stat["no_filing_rows"] += 1
                self.rejections.append({"source": source, "line": line, "company_id": stock,
                                        "period_end": period.end, "reason": "no_filing"})
                continue
            basis = row[spec["basis_field"]]
            if basis not in spec["valid_basis"] or row[spec["currency_field"]] != "KRW":
                raise ValueError(f"unexpected accounting basis/currency at line {line}")
            try:
                public = published_on(row[spec["public_field"]], row[spec["receipt_field"]], period)
            except ValueError as exc:
                self.rejections.append({"source": source, "line": line, "company_id": stock,
                                        "period_end": period.end, "reason": str(exc)})
                continue
            for mapping in spec["mappings"]:
                try:
                    value = number(row[mapping["field"]], mapping)
                except ValueError as exc:
                    self.rejections.append({"source": source, "line": line, "company_id": stock,
                                            "period_end": period.end, "node_id": mapping["node_id"],
                                            "reason": str(exc)})
                    continue
                evidence = {"file": spec["file"], "line": line, "field": mapping["field"],
                            "raw_value": row[mapping["field"]], "rcept_no": row[spec["receipt_field"]],
                            "public_field": spec["public_field"], "report_type": row[spec["report_field"]],
                            "calendar_quarter": row[spec["period_field"]],
                            "fiscal_year_end_month": row[spec["fiscal_month_field"]],
                            "reported_basis": basis, "reported_currency": row[spec["currency_field"]]}
                p = Point(stock, company["corp_code"], mapping["node_id"], period, value,
                          mapping["unit"], mapping["measure"], basis, public, source,
                          spec["priority"], evidence)
                self.raw[stock].append(p)
                self.confirmed.add(mapping["node_id"])
                stat["valid_fields"] += 1
        raw = [p for ps in self.raw.values() for p in ps]
        stat["period_end_range"] = [min(p.period.end for p in raw), max(p.period.end for p in raw)]
        stat["available_at_range"] = [min(p.available_at for p in raw), max(p.available_at for p in raw)]

    def resolve(self, company, as_of):
        candidates, resolved, facts, issues = defaultdict(list), defaultdict(dict), {}, Counter()
        cutoff = cutoff_for(as_of)
        for p in self.raw[company]:
            if p.available_at <= cutoff:
                candidates[p.node_id, p.slot].append(p)
        for (node, slot), versions in candidates.items():
            rank = max((p.available_at, -p.priority) for p in versions)
            best = [p for p in versions if (p.available_at, -p.priority) == rank]
            if len({(p.value, p.unit, p.measure) for p in best}) != 1:
                issues[node, "conflicting_revisions"] += 1
                continue
            chosen = min(best, key=lambda p: p.fact_id)
            resolved[node][slot] = chosen
            facts[chosen.fact_id] = chosen
        for f in self.formulas:
            node = f["node_id"]
            inputs = [resolved[n] for n in f["inputs"]]
            slots = set.intersection(*(set(series) for series in inputs))
            if not slots:
                issues[node, "missing_inputs_or_no_common_period_basis"] += 1
            for slot in slots:
                if slot in resolved[node]:
                    issues[node, "direct_observation_preferred"] += 1
                    continue
                terms = [series[slot] for series in inputs]
                if (len({(p.unit, p.measure) for p in terms}) != 1
                        or (f["unit"], f["measure"]) != ("fraction", "quarter_flow_ratio")
                        or terms[0].measure != "quarter_flow"):
                    issues[node, "incompatible_units_or_measure"] += 1
                    continue
                try:
                    result = FORMULAS[f["function"]][1](*(p.value for p in terms))
                    if not math.isfinite(result):
                        raise ValueError("nonfinite_formula")
                except (ZeroDivisionError, OverflowError, ValueError) as exc:
                    issues[node, "zero_denominator" if isinstance(exc, ZeroDivisionError)
                           else "invalid_formula_value"] += 1
                    continue
                point = Point(company, terms[0].corp_code, node, slot[1], result, f["unit"],
                              f["measure"], slot[0], max(p.available_at for p in terms),
                              "formula", 100, {"function": f["function"], "inputs": f["inputs"]},
                              tuple(p.fact_id for p in terms))
                resolved[node][slot] = point
                facts[point.fact_id] = point
        return resolved, facts, issues
