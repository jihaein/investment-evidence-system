"""OpenAI Responses adapter. Project .env settings take priority over inherited environment."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from threading import Lock
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_MODEL = "gpt-6-luna"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
CALLS = []
_ACTIVE_RECORDER = ContextVar("openai_call_recorder", default=None)


class CallRecorder:
    """Thread-safe API events for one pipeline request."""
    def __init__(self):
        self._calls = []
        self._lock = Lock()

    def append(self, event):
        with self._lock:
            self._calls.append(event)

    def mark(self):
        with self._lock:
            return len(self._calls)

    def tag_since(self, mark, stage):
        with self._lock:
            for event in self._calls[mark:]:
                event["stage"] = stage

    def snapshot(self, mark=0):
        with self._lock:
            return deepcopy(self._calls[mark:])


@contextmanager
def capture_calls():
    """Isolate API metrics for one request; worker contexts may share this recorder."""
    recorder = CallRecorder()
    token = _ACTIVE_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE_RECORDER.reset(token)


def _record_call(event):
    # CALLS is retained for older diagnostics only. Runtime aggregation uses capture_calls().
    CALLS.append(event)
    recorder = _ACTIVE_RECORDER.get()
    if recorder is not None:
        recorder.append(event)

class ProviderError(RuntimeError):
    pass

def setting(name, default=None):
    """Read .env on demand, independent of cwd; never mutate the process environment."""
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            if not separator or key.strip() != name:
                continue
            value = value.strip()
            if value.startswith(("'", '"')):
                end = value.find(value[0], 1)
                if end < 0:
                    raise ProviderError(f"Invalid quoted {name} in .env")
                return value[1:end]
            return re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return os.getenv(name, default)

def configured_model(fallback=None, *, stage=None):
    if stage is not None:
        if stage not in {"S4", "S5", "S7", "S10"}:
            raise ValueError("Unknown LLM stage")
        selected = setting(f"OPENAI_MODEL_{stage}")
        if selected:
            return selected
    return setting("OPENAI_MODEL") or fallback or DEFAULT_MODEL

def load_key():
    key = (setting("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ProviderError("OPENAI_API_KEY is required in project .env or environment")
    return key

def call_timing(calls, total_seconds):
    """Count overlapping request intervals once; keep aggregate work separately."""
    work = sum(call.get("seconds", 0) for call in calls)
    intervals = [(call["started_monotonic"], call["finished_monotonic"]) for call in calls
                 if "started_monotonic" in call and "finished_monotonic" in call]
    if len(intervals) != len(calls):
        return dict(api_seconds=None, api_work_seconds=work, python_seconds=None,
                    api_timing_method="unavailable_missing_request_intervals")
    elapsed, last_end = 0.0, None
    for start, end in sorted(intervals):
        elapsed += max(0.0, end - max(start, last_end if last_end is not None else start))
        last_end = max(end, last_end if last_end is not None else end)
    return dict(api_seconds=elapsed, api_work_seconds=work, python_seconds=max(0.0, total_seconds - elapsed),
                api_timing_method="union_of_request_intervals")

def strict_schema(schema):
    schema = deepcopy(schema)
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                properties = node.get("properties", {})
                required = node.get("required", [])
                for name, child in properties.items():
                    if name not in required:
                        properties[name] = {"anyOf": [child, {"type": "null"}]}
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    return schema

def generate(instructions, input_json, schema, *, model=None, timeout=120, retries=1,
             s4_cache_prefix=False, s4_cache_disabled=False):
    model = model or configured_model()
    key = load_key()
    wrapped = schema.get("type") != "object"
    wire_schema = ({"type": "object", "properties": {"result": schema}, "required": ["result"],
                    "additionalProperties": False} if wrapped else schema)
    body = dict(model=model, instructions=instructions, input=input_json, store=False,
                text={"format": {"type": "json_schema", "name": "fds_output",
                                 "strict": True, "schema": strict_schema(wire_schema)}})
    if s4_cache_disabled:
        body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    if s4_cache_prefix and isinstance(dynamic := json.loads(input_json), dict) and "node_dictionary" in dynamic:
        nodes = dynamic.pop("node_dictionary")
        fixed = instructions + "\n\n" + json.dumps({"node_dictionary": nodes}, ensure_ascii=False,
                                                   separators=(",", ":"))
        key_hash = sha256((model + "\n" + fixed).encode("utf-8")).hexdigest()
        body.pop("instructions")
        body["input"] = [
            {"role": "developer", "content": [{"type": "input_text", "text": fixed,
               "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "30m"}}]},
            {"role": "user", "content": [{"type": "input_text",
               "text": json.dumps(dynamic, ensure_ascii=False, separators=(",", ":"))}]}]
        body["prompt_cache_key"] = "fds-s4-" + key_hash
        body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    for attempt in range(retries + 1):
        start = time.perf_counter()
        event = dict(model=model, attempt=attempt + 1, outcome="failed")
        try:
            req = Request("https://api.openai.com/v1/responses", json.dumps(body).encode(),
                          {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urlopen(req, timeout=timeout) as response:
                payload = json.load(response)
            if payload.get("status") != "completed":
                raise ProviderError("OpenAI response incomplete")
            parts = [part for output in payload.get("output", []) for part in output.get("content", [])]
            if any(p.get("type") == "refusal" for p in parts):
                raise ProviderError("OpenAI response refused")
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "output_text")
            result = json.loads(text)
            event.update(outcome="completed", usage=payload.get("usage"))
            return json.dumps(result["result"] if wrapped else result, ensure_ascii=False)
        except HTTPError as exc:
            event["error"] = "HTTP " + str(exc.code)
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise ProviderError(event["error"]) from None
        except (URLError, OSError):
            event["error"] = "connection_or_timeout"
            if attempt == retries:
                raise ProviderError(event["error"]) from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("OpenAI response envelope invalid") from None
        finally:
            end = time.perf_counter()
            event.update(seconds=end - start, started_monotonic=start, finished_monotonic=end)
            _record_call(event)
        time.sleep(min(2 ** attempt, 4))
