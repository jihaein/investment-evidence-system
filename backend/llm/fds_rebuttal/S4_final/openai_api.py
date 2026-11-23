"""OpenAI Responses adapter. Credentials are read only from the environment."""
from copy import deepcopy
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_MODEL = "gpt-4.1-mini"
CALLS = []

class ProviderError(RuntimeError):
    pass

def load_key():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ProviderError("OPENAI_API_KEY environment variable is required")
    return key

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

def sampling_temperature(model):
    """Same conversation → same verdict: sample at 0 unless OPENAI_TEMPERATURE says otherwise.
    Reasoning models (o-series, gpt-5) reject the parameter, so it is left out for them."""
    if model.startswith(("o1", "o3", "o4", "gpt-5")):
        return None
    value = os.getenv("OPENAI_TEMPERATURE", "").strip()
    return float(value) if value else 0.0


def generate(instructions, input_json, schema, *, model=None, timeout=120, retries=1):
    model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    key = load_key()
    wrapped = schema.get("type") != "object"
    wire_schema = ({"type": "object", "properties": {"result": schema}, "required": ["result"],
                    "additionalProperties": False} if wrapped else schema)
    body = dict(model=model, instructions=instructions, input=input_json, store=False,
                text={"format": {"type": "json_schema", "name": "fds_output",
                                 "strict": True, "schema": strict_schema(wire_schema)}})
    temperature = sampling_temperature(model)
    if temperature is not None:
        body["temperature"] = temperature
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
            event["seconds"] = time.perf_counter() - start
            CALLS.append(event)
        time.sleep(min(2 ** attempt, 4))
