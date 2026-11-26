"""Small shared input policy; no stage orchestration."""
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from hashlib import sha256
from pathlib import Path
import re

_JSON_CACHE = {}
_SELECTION_CACHE = {}
_HEADER_CACHE = {}
_HASH_CACHE = {}
_CARD_CACHE = {}

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))


def config():
    """runtime_config.json; REBUTTAL_S3_DIR (absolute path) replaces company_arguments_dir on servers whose S3 files are
    restored from cloud storage instead of the Git LFS files (ops/cloud/datasets.json rebuttal_s3)."""
    value = json.loads((ROOT / "runtime_config.json").read_text(encoding="utf-8"))
    override = os.environ.get("REBUTTAL_S3_DIR", "").strip()
    if override:
        value["company_arguments_dir"] = override
    return value


def card_allowed(card):
    """Card review status is retained as metadata, not an evidence gate."""
    return bool(card) and card.get("review_status") in {"approved", "draft"}


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD date")
    return date.fromisoformat(value)


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("availability timestamp must include timezone")
    return result


def company_id(request):
    code = request.get("company_id", request.get("stock_code"))
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("company_id must be a six-digit string")
    if "stock_code" in request and request["stock_code"] != code:
        raise ValueError("company_id/stock_code mismatch")
    return code


def _signature(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns, stat.st_size


def cached_json(path):
    """Cache immutable runtime inputs and invalidate them when the file changes."""
    path = Path(path).resolve()
    signature = _signature(path)
    entry = _JSON_CACHE.get(path)
    if entry is None or entry[0] != signature:
        entry = signature, json.loads(path.read_text(encoding="utf-8-sig"))
        _JSON_CACHE[path] = entry
    return entry[1]


def file_sha256(path):
    path = Path(path).resolve()
    signature = _signature(path)
    entry = _HASH_CACHE.get(path)
    if entry is None or entry[0] != signature:
        entry = signature, sha256(path.read_bytes()).hexdigest()
        _HASH_CACHE[path] = entry
    return entry[1]


def card_bundle(directory):
    """Return immutable cards and their aggregate digest after one verified load."""
    directory = Path(directory).resolve()
    paths = sorted(directory.glob("*.json"))
    signature = tuple((str(path), *_signature(path)) for path in paths)
    entry = _CARD_CACHE.get(directory)
    if entry is not None and entry[0] == signature:
        return entry[1], entry[2]
    cards, hashes = {}, {}
    for path in paths:
        card = cached_json(path)
        edge_id = card["edge_id"]
        if edge_id in cards:
            raise ValueError(f"duplicate edge card: {edge_id}")
        cards[edge_id] = card
        hashes[path.name] = file_sha256(path)
    digest = sha256(json.dumps(hashes, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode()).hexdigest()
    _CARD_CACHE[directory] = signature, cards, digest
    return cards, digest


def _snapshot_headers(directory):
    """Read only the small metadata prefix of each large S3 snapshot."""
    directory = Path(directory).resolve()
    paths = sorted(directory.glob("company.argument*.json"))
    signature = tuple((str(path), *_signature(path)) for path in paths)
    entry = _HEADER_CACHE.get(directory)
    if entry is not None and entry[0] == signature:
        return entry[1]
    headers = []
    fields = ("as_of", "effective_from", "effective_to")
    for path in paths:
        with path.open("rb") as stream:
            prefix = stream.read(262144).decode("utf-8-sig")
        values = {}
        for field in fields:
            match = re.search(rf'"{field}"\s*:\s*"([^"]+)"', prefix)
            if not match:
                raise ValueError(f"S3 snapshot metadata missing: {path.name}:{field}")
            values[field] = match.group(1)
        headers.append((path.resolve(), values))
    _HEADER_CACHE[directory] = signature, headers
    return headers


def select_company_argument(code, alert_date, directory=None):
    """Select exactly one effective snapshot. Date-only alert means start of day KST.

    Check every referenced fact, including derived inputs; never fall back to
    another snapshot when the matching snapshot fails evidence validation.
    """
    code = company_id({"company_id": code})
    day = parse_date(alert_date)
    at = datetime.combine(day, time.min, KST)
    directory = Path(directory) if directory is not None else ROOT / config()["company_arguments_dir"]
    matches = [(path, header) for path, header in _snapshot_headers(directory)
               if parse_date(header["effective_from"]) <= day <= parse_date(header["effective_to"])]
    if len(matches) != 1:
        raise ValueError(f"expected one effective company argument at {alert_date}; found {len(matches)}")
    path, header = matches[0]
    registry_name = "fact_registry.json"
    cache_key = (str(Path(directory).resolve()), code, alert_date, str(path), _signature(path))
    prior = _SELECTION_CACHE.get(cache_key)
    if prior is not None:
        registry = path.parent / prior[1]["s3_metadata"]["fact_registry"]
        if prior[2] == _signature(registry):
            return prior[0], prior[1]
    data = cached_json(path)
    if code not in data["companies"]:
        raise ValueError(f"company argument has no company_id: {code}")
    if any(data[field] != header[field] for field in ("as_of", "effective_from", "effective_to")):
        raise ValueError("S3 snapshot header mismatch")
    co = data["companies"][code]
    cutoff = instant(data["s3_metadata"]["cutoff"])
    if (parse_date(data["as_of"]) > day or cutoff > at or
            co["company_id"] != code or co["as_of"] != data["as_of"]):
        raise ValueError("company/date/cutoff mismatch")
    if "available_at" in data and instant(data["available_at"]) > at:
        raise ValueError("snapshot not yet available")
    registry = path.parent / data["s3_metadata"]["fact_registry"]
    facts = cached_json(registry)["facts"]
    checked, visiting = set(), set()

    def check_fact(fid):
        if fid in checked:
            return facts[fid]
        if fid in visiting:
            raise ValueError("cyclic fact inputs")
        visiting.add(fid)
        f = facts.get(fid)
        if (not f or f["fact_id"] != fid or f["company_id"] != code or
                instant(f["available_at"]) > min(cutoff, at) or
                parse_date(f["period_end"]) > min(day, cutoff.astimezone(KST).date())):
            raise ValueError(f"invalid or future fact: {fid}")
        for parent in f.get("input_fact_ids", []):
            check_fact(parent)
        visiting.remove(fid)
        checked.add(fid)
        return f

    for nid, obs in co["nodes"].items():
        if obs["status"] == "observed" and not obs["fact_ids"]:
            raise ValueError("observed node without facts")
        for fid in obs["fact_ids"]:
            f = check_fact(fid)
            if f["node_id"] != nid or obs["series"].get(f["period_end"]) != f["value"]:
                raise ValueError("fact does not match node/series")
    _SELECTION_CACHE[cache_key] = path, data, _signature(registry)
    return path, data
