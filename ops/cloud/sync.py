"""Mirror every data folder this project uses to Supabase Storage, and restore it.

    python ops/cloud/sync.py status            # what differs between this computer and the cloud
    python ops/cloud/sync.py push              # upload new/changed files (all datasets)
    python ops/cloud/sync.py pull              # download files missing on this computer (never overwrites)
    python ops/cloud/sync.py pull --overwrite  # also replace local files that differ from the cloud
    python ops/cloud/sync.py pull --dest /tmp/restore   # restore everything into an empty folder (test)
    python ops/cloud/sync.py push --only rawdata dart_snapshots
    python ops/cloud/sync.py prune             # show what no current dataset uses (dry run)
    python ops/cloud/sync.py prune --yes       # delete it permanently

Storage layout (private bucket, default "ies-data"):
    blobs/<aa>/<sha256>.gz[.pN]       file contents, gzip, content-addressed (identical files stored once;
                                      Korean file names never become object keys)
    manifests/<dataset>.json.gz       current file list: path -> sha256, size, mtime, parts
    manifests/history/<dataset>/<UTC time>.json.gz   every pushed version of the file list
    index/blobs.json.gz               hashes already uploaded -> number of parts

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env. push/pull never delete anything; only `prune --yes` deletes, and only in the cloud.
Folders are listed in ops/cloud/datasets.json.
"""
import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CHUNK = 40 * 1024 * 1024          # below Supabase's 50 MB free-plan object limit
SKIP_NAMES = {".DS_Store", ".converter.lock"}
SKIP_DIRS = {"__pycache__", ".run.lock", ".ipynb_checkpoints"}
WORKERS = 8


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, url, key, bucket):
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.local = threading.local()
        self.key = key

    @property
    def session(self):
        if not hasattr(self.local, "s"):
            s = requests.Session()
            s.headers["apikey"] = self.key
            if self.key.startswith("eyJ"):          # legacy JWT keys also go in Authorization;
                s.headers["Authorization"] = f"Bearer {self.key}"   # new sb_secret_ keys use apikey only
            self.local.s = s
        return self.local.s

    def request(self, method, path, **kwargs):
        error = None
        for attempt in range(6):
            try:
                response = self.session.request(method, f"{self.base}{path}", timeout=180, **kwargs)
            except requests.RequestException as exc:
                error = type(exc).__name__          # never print the URL/headers (key)
            else:
                if response.status_code < 500 and response.status_code != 429:
                    return response
                error = f"HTTP {response.status_code}"
            time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"Storage {method} {path.split('?')[0]} failed after retries: {error}")

    def ensure_bucket(self):
        response = self.request("GET", f"/bucket/{self.bucket}")
        if response.status_code == 200:
            if response.json().get("public"):
                raise SystemExit(f"Bucket {self.bucket} is PUBLIC. Make it private in Supabase first.")
            return
        response = self.request("POST", "/bucket", json={"id": self.bucket, "name": self.bucket, "public": False})
        if response.status_code not in (200, 201):
            raise SystemExit(f"Could not create bucket {self.bucket}: HTTP {response.status_code} {response.text[:200]}")
        print(f"Created private bucket {self.bucket}")

    def put(self, key, data):
        response = self.request("POST", f"/object/{self.bucket}/{key}", data=data,
                                headers={"x-upsert": "true", "Content-Type": "application/octet-stream"})
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Upload {key} failed: HTTP {response.status_code} {response.text[:200]}")

    def get(self, key):
        response = self.request("GET", f"/object/authenticated/{self.bucket}/{key}")
        if response.status_code == 200:
            return response.content
        if response.status_code in (400, 404) and "not" in response.text.lower() and "found" in response.text.lower():
            return None
        raise RuntimeError(f"Download {key} failed: HTTP {response.status_code} {response.text[:200]}")

    def list_prefix(self, prefix):
        """Yield (name, size) for objects directly under prefix (one folder level)."""
        offset = 0
        while True:
            response = self.request("POST", f"/object/list/{self.bucket}",
                                    json={"prefix": prefix, "limit": 1000, "offset": offset})
            if response.status_code != 200:
                raise RuntimeError(f"List {prefix} failed: HTTP {response.status_code} {response.text[:200]}")
            items = response.json()
            for item in items:
                if item.get("id"):                       # folders have no id
                    yield item["name"], (item.get("metadata") or {}).get("size", 0)
            if len(items) < 1000:
                return
            offset += 1000

    def delete(self, keys):
        for i in range(0, len(keys), 500):
            response = self.request("DELETE", f"/object/{self.bucket}", json={"prefixes": keys[i:i + 500]})
            if response.status_code != 200:
                raise RuntimeError(f"Delete failed: HTTP {response.status_code} {response.text[:200]}")

    def get_json(self, key):
        data = self.get(key)
        return None if data is None else json.loads(gzip.decompress(data))

    def put_json(self, key, payload):
        self.put(key, gzip.compress(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()))


# ---------------------------------------------------------------- datasets
def load_datasets(only=None):
    config = json.loads((HERE / "datasets.json").read_text(encoding="utf-8"))
    datasets = []
    for item in config:
        if only and item["name"] not in only:
            continue
        raw = os.getenv(item["env"]) if item.get("env") else None
        raw = raw or item.get("path") or item.get("default")
        if not raw:
            print(f"- {item['name']}: skipped ({item.get('env')} not set)")
            continue
        root = Path(os.path.expanduser(raw))
        root = root if root.is_absolute() else (ROOT / root)
        datasets.append({**item, "root": root.resolve()})
    unknown = set(only or ()) - {d["name"] for d in config}
    if unknown:
        raise SystemExit(f"Unknown dataset(s): {sorted(unknown)}")
    return datasets


def nfc(text):
    """One spelling for Korean paths: macOS writes names decomposed (NFD), Linux keeps them as given."""
    return unicodedata.normalize("NFC", text)


def remote_files(store, name):
    """The current file list of a dataset, with every path in NFC."""
    files = (store.get_json(f"manifests/{name}.json.gz") or {}).get("files", {})
    return {nfc(rel): entry for rel, entry in files.items()}


def wanted(rel, dataset):
    include, exclude = dataset.get("include"), dataset.get("exclude", [])
    if include and not any(fnmatch.fnmatch(rel, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatch(rel, pattern) for pattern in exclude)


def scan(dataset):
    root = dataset["root"]
    files = {}
    if not root.exists():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name in SKIP_NAMES or name.endswith(".lock") or name.startswith(".tmp"):
                continue
            path = Path(dirpath) / name
            rel = nfc(path.relative_to(root).as_posix())
            if wanted(rel, dataset):
                files[rel] = path
    return files


class HashCache:
    """Remember sha256 by (size, mtime) so repeat runs don't re-read 1 GB."""
    def __init__(self):
        self.path = HERE / ".hashcache.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.lock = threading.Lock()

    def sha(self, path):
        st = path.stat()
        key = str(path)
        hit = self.data.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2], st
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        with self.lock:
            self.data[key] = [st.st_size, st.st_mtime_ns, digest.hexdigest()]
        return digest.hexdigest(), st

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


def blob_keys(sha, parts):
    base = f"blobs/{sha[:2]}/{sha}.gz"
    return [base] if parts == 1 else [f"{base}.p{i}" for i in range(parts)]


# ---------------------------------------------------------------- commands
def local_manifest(dataset, cache):
    files = scan(dataset)
    manifest = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(cache.sha, path): rel for rel, path in files.items()}
        for future in as_completed(futures):
            sha, st = future.result()
            manifest[futures[future]] = {"sha256": sha, "size": st.st_size, "mtime": st.st_mtime}
    return files, manifest


def push(store, datasets, cache):
    index = store.get_json("index/blobs.json.gz") or {}       # sha256 -> number of parts
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    total_new = 0
    try:
        for dataset in datasets:
            if dataset.get("archived"):
                continue                      # cloud-only: the local copy was removed on purpose
            files, manifest = local_manifest(dataset, cache)
            if not files:
                print(f"- {dataset['name']}: no files at {dataset['root']} (skipped)")
                continue
            remote = remote_files(store, dataset["name"])
            todo = {}
            for rel, entry in manifest.items():
                if entry["sha256"] not in index:
                    todo.setdefault(entry["sha256"], rel)

            def upload(item):
                sha, rel = item
                data = gzip.compress(files[rel].read_bytes(), compresslevel=6)
                if hashlib.sha256(gzip.decompress(data)).hexdigest() != sha:
                    raise RuntimeError(f"{rel} changed while uploading; rerun push")
                parts = max(1, -(-len(data) // CHUNK))
                for i, key in enumerate(blob_keys(sha, parts)):
                    store.put(key, data[i * CHUNK:(i + 1) * CHUNK])
                return sha, parts, len(data)

            sent = 0
            with ThreadPoolExecutor(WORKERS) as pool:
                futures = [pool.submit(upload, item) for item in todo.items()]
                for n, future in enumerate(as_completed(futures), 1):
                    sha, parts, size = future.result()
                    index[sha] = parts
                    sent += size
                    if n % 250 == 0 or n == len(futures):
                        print(f"  {dataset['name']}: uploaded {n:,}/{len(futures):,} ({sent / 1e6:,.0f} MB)", flush=True)
            for entry in manifest.values():
                entry["parts"] = index[entry["sha256"]]
            payload = {"dataset": dataset["name"], "source_root": str(dataset["root"]), "pushed_at": stamp,
                       "files": manifest}
            store.put_json(f"manifests/{dataset['name']}.json.gz", payload)
            store.put_json(f"manifests/history/{dataset['name']}/{stamp}.json.gz", payload)
            changed = sum(1 for rel, e in manifest.items() if remote.get(rel, {}).get("sha256") != e["sha256"])
            gone = len(set(remote) - set(manifest))
            print(f"✓ {dataset['name']}: {len(manifest):,} files, {changed:,} new/changed, "
                  f"{len(todo):,} blobs uploaded" + (f", {gone:,} no longer local (kept in cloud)" if gone else ""))
            total_new += len(todo)
    finally:
        store.put_json("index/blobs.json.gz", index)
        cache.save()
    print(f"Push complete: {total_new:,} new blobs.")


def pull(store, datasets, cache, dest=None, overwrite=False, only=None):
    failures = 0
    for dataset in datasets:
        if dataset.get("archived") and dataset["name"] not in (only or ()):
            continue                          # restore archived data only when asked by name
        remote = store.get_json(f"manifests/{dataset['name']}.json.gz")
        if not remote:
            print(f"- {dataset['name']}: nothing in the cloud yet")
            continue
        root = (Path(dest) / dataset["name"]) if dest else dataset["root"]
        remote["files"] = {nfc(rel): entry for rel, entry in remote["files"].items()}
        todo = []
        for rel, entry in remote["files"].items():
            target = root / rel
            if target.is_file():
                if not overwrite:
                    continue          # never replace a local file unless asked (local may be newer)
                if target.stat().st_size == entry["size"] and cache.sha(target)[0] == entry["sha256"]:
                    continue
            todo.append((rel, entry))

        def download(item):
            rel, entry = item
            data = b"".join(store.get(key) or b"" for key in blob_keys(entry["sha256"], entry.get("parts") or 1))
            content = gzip.decompress(data)
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise RuntimeError(f"Checksum mismatch for {rel}")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".tmp-", delete=False) as handle:
                handle.write(content)
                partial = Path(handle.name)
            partial.replace(target)
            os.utime(target, (entry["mtime"], entry["mtime"]))
            return rel

        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(download, item): item[0] for item in todo}
            for n, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()
                except Exception as exc:          # report every failure, keep going
                    failures += 1
                    print(f"  ! {futures[future]}: {exc}")
                if n % 250 == 0 or n == len(futures):
                    print(f"  {dataset['name']}: downloaded {n:,}/{len(futures):,}", flush=True)
        print(f"✓ {dataset['name']}: {len(remote['files']):,} files in cloud, {len(todo):,} restored into {root}")
    cache.save()
    if failures:
        raise SystemExit(f"{failures} file(s) failed to restore")


def prune(store, yes=False):
    """Remove from the cloud anything no current dataset uses.

    Datasets no longer listed in datasets.json are retired (their file list moves to
    manifests/retired/), then every blob that no current or past upload of a listed dataset
    (archived ones included) refers to is deleted. Retired datasets can no longer be restored.
    """
    config = {item["name"] for item in json.loads((HERE / "datasets.json").read_text(encoding="utf-8"))}
    current = {name[:-len(".json.gz")] for name, _ in store.list_prefix("manifests/") if name.endswith(".json.gz")}
    retire = sorted(current - config)
    keep = set()
    for name in sorted(current & config):
        lists = [f"manifests/{name}.json.gz"] + [f"manifests/history/{name}/{item}"
                                                for item, _ in store.list_prefix(f"manifests/history/{name}/")]
        for key in lists:                     # every past upload of a kept dataset stays restorable
            for entry in (store.get_json(key) or {}).get("files", {}).values():
                keep.update(blob_keys(entry["sha256"], entry.get("parts") or 1))
    blobs = {}
    for shard in range(256):
        prefix = f"blobs/{shard:02x}/"
        for name, size in store.list_prefix(prefix):
            blobs[prefix + name] = size
    unused = sorted(key for key in blobs if key not in keep)
    freed = sum(blobs[key] for key in unused)
    total = sum(blobs.values())
    print(f"Datasets to retire: {retire or 'none'}")
    print(f"Stored blobs: {len(blobs):,} ({total / 1e6:,.0f} MB). Unused: {len(unused):,} ({freed / 1e6:,.0f} MB). "
          f"After cleanup: {(total - freed) / 1e6:,.0f} MB")
    if not yes:
        print("Dry run: nothing deleted. Re-run with --yes to delete permanently.")
        return
    for name in retire:
        payload = store.get_json(f"manifests/{name}.json.gz")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        store.put_json(f"manifests/retired/{name}/{stamp}.json.gz", payload)
        store.delete([f"manifests/{name}.json.gz"])
        print(f"Retired dataset {name}")
    store.delete(unused)
    index = store.get_json("index/blobs.json.gz") or {}
    gone = {key.split("/")[2].split(".gz")[0] for key in unused}
    store.put_json("index/blobs.json.gz", {sha: parts for sha, parts in index.items() if sha not in gone})
    print(f"Deleted {len(unused):,} unused blobs ({freed / 1e6:,.0f} MB).")


def status(store, datasets, cache):
    for dataset in datasets:
        if dataset.get("archived"):
            remote = (store.get_json(f"manifests/{dataset['name']}.json.gz") or {}).get("files", {})
            print(f"{dataset['name']:<18} ARCHIVED cloud only, {len(remote):,} files "
                  f"(restore: sync.py pull --only {dataset['name']} --dest <folder>)")
            continue
        _, manifest = local_manifest(dataset, cache)
        remote = remote_files(store, dataset["name"])
        new = sum(1 for r in manifest if r not in remote)
        changed = sum(1 for r, e in manifest.items() if r in remote and remote[r]["sha256"] != e["sha256"])
        cloud_only = sum(1 for r in remote if r not in manifest)
        size = sum(e["size"] for e in manifest.values()) / 1e6
        state = "IN SYNC" if not (new or changed or cloud_only) else "DIFFERS"
        print(f"{dataset['name']:<18} {state:<8} local {len(manifest):>6,} files {size:>8,.0f} MB | "
              f"cloud {len(remote):>6,} | not uploaded {new + changed:,} | cloud-only {cloud_only:,}   [{dataset['root']}]")
    cache.save()


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "push", "pull", "prune"))
    parser.add_argument("--only", nargs="+", help="dataset names from ops/cloud/datasets.json")
    parser.add_argument("--yes", action="store_true", help="prune: actually delete (default is a dry run)")
    parser.add_argument("--dest", help="pull: restore under this folder instead of the configured paths")
    parser.add_argument("--overwrite", action="store_true",
                        help="pull: also replace local files that differ from the cloud (default: only fill in missing files)")
    args = parser.parse_args()
    missing = [name for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Add {', '.join(missing)} to .env (Supabase → Project Settings → API).")
    store = Store(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                  os.getenv("CLOUD_BUCKET", "ies-data"))
    store.ensure_bucket()
    datasets = load_datasets(args.only)
    cache = HashCache()
    if args.command == "prune":
        prune(store, args.yes)
    elif args.command == "push":
        push(store, datasets, cache)
    elif args.command == "pull":
        pull(store, datasets, cache, args.dest, args.overwrite, args.only)
    else:
        status(store, datasets, cache)


if __name__ == "__main__":
    sys.exit(main())
