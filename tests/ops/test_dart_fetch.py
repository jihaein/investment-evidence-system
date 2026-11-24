"""data_pipeline/fetch/fetch_disclosures.py fetch: a dropped connection is tried again; the key never shows."""
import pytest
import requests

from data_pipeline.fetch import fetch_disclosures as fd


class Resp:
    def __init__(self, status):
        self.status_code = status


class Flaky:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return Resp(o)


def test_a_timeout_then_success_is_one_good_call():
    s = Flaky([requests.ConnectTimeout("https://x?crtfc_key=SECRET"), 200])
    assert fd.fetch(s, "list.json", "SECRET", {}, waits=(0, 0)).status_code == 200
    assert s.calls == 2


def test_three_timeouts_fail_without_the_key():
    s = Flaky([requests.ConnectTimeout("k=SECRET")] * 3)
    with pytest.raises(RuntimeError) as e:
        fd.fetch(s, "list.json", "SECRET", {}, waits=(0, 0))
    assert "SECRET" not in str(e.value) and "ConnectTimeout" in str(e.value) and s.calls == 3


def test_a_5xx_is_retried_a_4xx_is_not():
    s = Flaky([503, 200])
    assert fd.fetch(s, "list.json", "k", {}, waits=(0, 0)).status_code == 200
    s = Flaky([404])
    with pytest.raises(RuntimeError):
        fd.fetch(s, "list.json", "k", {}, waits=(0, 0))
    assert s.calls == 1
