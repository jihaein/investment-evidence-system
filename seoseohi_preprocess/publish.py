"""Optional pointer publication. Consumer resolves current.json once per job."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .pipeline import SCHEMA, read_csv, validate_output, PreprocessError


def publish(release_dir, pointer_file, *, accept_review=False):
    release=Path(release_dir).resolve();pointer=Path(pointer_file).resolve()
    validate_output(release)
    manifest=json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    if manifest['review_count'] and not accept_review:
        raise PreprocessError('REVIEW_QUEUE_NOT_ACCEPTED: inspect review_queue.csv before publication')
    for name,digest in manifest['output_sha256'].items():
        if hashlib.sha256((release/name).read_bytes()).hexdigest()!=digest:
            raise PreprocessError('RELEASE_HASH_CHANGED')
    pointer.parent.mkdir(parents=True,exist_ok=True)
    lock=pointer.with_name(pointer.name+'.lock')
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise PreprocessError('PUBLICATION_ALREADY_RUNNING') from None
    os.close(fd)
    temp=None
    try:
        previous=json.loads(pointer.read_text(encoding='utf-8'))['release_dir'] if pointer.exists() else None
        changes=[]
        for name,schema in SCHEMA.items():
            keys=next((c['duplicate']['columns'] for c in schema['checks'] if 'duplicate' in c),None)
            before=read_csv(Path(previous)/name,two=True) if previous else []
            after=read_csv(release/name,two=True)
            if keys:
                old={tuple(r[k] for k in keys):r for r in before}
                new={tuple(r[k] for k in keys):r for r in after}
                for key in sorted(set(old)|set(new)):
                    a,b=old.get(key,{}),new.get(key,{})
                    for c in sorted(set(a)|set(b)):
                        if a.get(c)!=b.get(c):
                            changes.append({'file':name,'key':list(key),'column':c,'before':a.get(c),'after':b.get(c)})
            elif before!=after:
                # 09 permits repeated rows: preserve complete before/after arrays,
                # rather than invent a uniqueness key that loses transactions.
                changes.append({'file':name,'type':'MULTISET_WITH_ORDER','before':before,'after':after})
        payload={'release_dir':str(release),'previous_release_dir':previous,
                 'package_version':manifest['package_version'],'changes':changes}
        fd,temp=tempfile.mkstemp(prefix='.'+pointer.name,dir=pointer.parent)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(payload,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temp,pointer);temp=None
    finally:
        if temp:Path(temp).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('release_dir');p.add_argument('pointer_file')
    p.add_argument('--accept-review',action='store_true')
    a=p.parse_args();publish(a.release_dir,a.pointer_file,accept_review=a.accept_review)
