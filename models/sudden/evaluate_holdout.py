"""규칙을 동결한 뒤 수집한 기사에 대한 내용 검토 라벨과 예측을 비교한다.
기준 기간 없이 추출·묶음만 평가한다. 예측을 본 뒤 라벨을 자동 생성하지 않는다.
"""

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime,time
from itertools import combinations, permutations
from pathlib import Path
from types import SimpleNamespace

import naver_crawl as crawl
import news_facts as facts
import news_review as review

HERE = Path(__file__).resolve().parent


def article_hash(article):
    fields = {k:article.get(k) for k in ('url','title','body','published_at','modified_at','publisher')}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_case(cache, spec):
    day_sample=spec.get('sample_type')=='news_day'
    if day_sample:
        if spec['start']!=spec['end']:
            raise ValueError('뉴스 날짜 표본은 하루 단위로 지정합니다')
        day=datetime.fromisoformat(spec['start']).date()
        start=datetime.combine(day,time.min,tzinfo=crawl.KST)
        end=datetime.combine(day,time.max,tzinfo=crawl.KST)
        expected={(spec['start'],spec['query'])}
    else:
        with (HERE/'output/drop_events.csv').open(encoding='utf-8-sig',newline='') as f:
            event=next((e for e in csv.DictReader(f) if e['drop_id']==spec['drop_id']),None)
        if not event:
            raise ValueError('사건 ID 없음: '+spec['drop_id'])
        args=SimpleNamespace(stock=spec['stock'],start=spec['start'],end=spec['end'],max_priority=1)
        expected={(r['date'],q) for r,q in crawl.tasks(HERE/'output/crawl_plan.csv',args)}
    with sqlite3.connect(cache/'crawl.sqlite3') as db:
        searches=[json.loads(p) for p, in db.execute('SELECT payload FROM searches')]
        articles={url:json.loads(p) for url,p in db.execute('SELECT url,payload FROM articles')}
    selected={(s['date'],s['query']):s for s in searches if s['stock_code']==spec['stock'] and (s['date'],s['query']) in expected}
    unfinished=[{'date':d,'query':q,'status':selected.get((d,q),{}).get('status','missing')}
                for d,q in sorted(expected) if selected.get((d,q),{}).get('status') not in ('complete','empty')]
    urls={r['naver_url'] for s in selected.values() for r in s.get('results',[]) if r['naver_url']}
    missing=[url for url in sorted(urls) if articles.get(url,{}).get('status') not in ('complete','excluded_automated')]
    population,excluded=[],[]
    for url in sorted(urls):
        a=articles.get(url,{})
        if a.get('status')!='complete':
            continue
        if day_sample:
            period='news_day'
            published=a.get('published_at')
            if not published:
                status='time_unknown'
            elif not start<=datetime.fromisoformat(published)<=end:
                status='outside_period'
            elif a.get('modified_at') and datetime.fromisoformat(a['modified_at'])>end:
                status='modified_after_cutoff'
            else:
                status='eligible_by_timestamp'
        else:
            period,status=review.period_of(a,event)
        if period in ('event_window','news_day') and status=='eligible_by_timestamp':
            population.append(a)
        else:
            excluded.append({'url':url,'reason':status if period in ('event_window','news_day') else 'outside_event_window','period':period})
    coverage={'expected_searches':len(expected),'completed_searches':len(expected)-len(unfinished),
              'unfinished_searches':unfinished,'unavailable_articles':missing,
              'automated_body_exclusions':sum(articles.get(u,{}).get('status')=='excluded_automated' for u in urls),
              'external_only_results':sum(not r['naver_url'] for s in selected.values() for r in s.get('results',[])),
              'time_exclusions':excluded,'collection_complete':not unfinished and not missing}
    return population,coverage


def fact_errors(extracted,label):
    errors=[]
    for key,value in label.get('expected',{}).items():
        actual=extracted['fields'].get(key[7:]) if key.startswith('fields.') else extracted.get(key)
        allowed=[value]+label.get('accepted_values',{}).get(key,[])
        if actual not in allowed:
            errors.append(f'{key}: expected={value!r}, actual={actual!r}')
    for key in label.get('absent_fields',[]):
        if key in extracted['fields']:
            errors.append(f'{key}: expected absent, actual={extracted["fields"][key]!r}')
    return errors


def valid_fact_label(label):
    return (isinstance(label,dict) and isinstance(label.get('expected'),dict)
            and isinstance(label.get('accepted_values',{}),dict)
            and all(isinstance(v,list) for v in label.get('accepted_values',{}).values())
            and isinstance(label.get('absent_fields',[]),list))


def evaluate(articles, labels):
    predictions={a['url']:facts.extract(a) for a in articles}
    groups=facts.cluster(articles)
    group_by_url={m['url']:g['group_id'] for g in groups if g.get('primary_fact') for m in g['members']}
    rows=[]
    field_checks={}
    for a in articles:
        label=labels[a['url']]
        extracted=predictions[a['url']]['primary']
        candidates=facts.fact_list(predictions[a['url']])
        if label['has_primary_fact'] and not label.get('requires_split'):
            for key,value in label['expected'].items():
                counter=field_checks.setdefault(key,{'checked':0,'matched':0,'abstained':0,'missing':0})
                counter['checked']+=1
                if not extracted:
                    counter['abstained']+=1
                else:
                    actual=extracted['fields'].get(key[7:]) if key.startswith('fields.') else extracted.get(key)
                    counter['matched']+=actual in [value]+label.get('accepted_values',{}).get(key,[])
                    counter['missing']+=actual is None
        errors=[]
        if candidates and not label['has_primary_fact']:
            errors.append('spurious_fact')
        elif candidates and label.get('requires_split'):
            expected_facts=label.get('expected_facts',[])
            if len(candidates)==1:
                errors.append('multiple_primary_facts_not_representable')
            if not expected_facts:
                errors.append('multiple_fact_labels_missing')
            elif len(candidates)!=len(expected_facts) or len(candidates)>6:
                errors.append('fact_cardinality_mismatch')
            else:
                errors+=min(([e for f,l in zip(order,expected_facts) for e in fact_errors(f,l)]
                             for order in permutations(candidates)),key=len)
        elif candidates:
            if len(candidates)!=1:
                errors.append('unexpected_multiple_facts')
            else:
                errors+=fact_errors(candidates[0],label)
        rows.append({'url':a['url'],'title':a['title'],'has_primary_fact':label['has_primary_fact'],
                     'expected_group':label.get('group'), 'extracted':bool(candidates),
                     'correct_extraction':bool(candidates) and label['has_primary_fact'] and not errors,
                     'errors':errors, 'hold_reason':predictions[a['url']]['reason'], 'prediction':extracted,
                     'predictions':candidates})
    tp=fp=fn=abstained_positive=abstained_pairs=0
    pair_errors=[]
    factual=[a['url'] for a in articles if labels[a['url']]['has_primary_fact'] and labels[a['url']].get('group') and not labels[a['url']].get('requires_split')]
    for a,b in combinations(factual,2):
        same_truth=labels[a]['group']==labels[b]['group']
        if a not in group_by_url or b not in group_by_url:
            abstained_pairs+=1
            abstained_positive+=same_truth
            continue
        same_prediction=group_by_url[a]==group_by_url[b]
        if same_truth and same_prediction:
            tp+=1
        elif same_prediction:
            fp+=1
            pair_errors.append({'a':a,'b':b,'error':'false_merge'})
        elif same_truth:
            fn+=1
            pair_errors.append({'a':a,'b':b,'error':'false_split'})
    fact_count=sum(r['has_primary_fact'] for r in rows)
    correct=sum(r['correct_extraction'] for r in rows)
    extracted_count=sum(r['extracted'] for r in rows)
    return {'article_count':len(rows),'primary_fact_articles':fact_count,'extracted_articles':extracted_count,
            'field_checks_single_fact_articles':field_checks,
            'multi_fact_articles':sum(bool(labels[a['url']].get('requires_split')) for a in articles),
            'correct_extractions':correct,'extraction_precision':correct/extracted_count if extracted_count else None,
            'fact_coverage':correct/fact_count if fact_count else None,
            'held_fact_articles':sum(r['has_primary_fact'] and not r['extracted'] for r in rows),
            'field_error_articles':sum(bool(r['errors']) for r in rows),
            'grouping':{'true_positive_pairs':tp,'false_merge_pairs':fp,'false_split_pairs':fn,
                        'abstained_pairs':abstained_pairs,'abstained_positive_pairs':abstained_positive,
                        'precision':tp/(tp+fp) if tp+fp else None,
                        'recall':tp/(tp+fn+abstained_positive) if tp+fn+abstained_positive else None},
            'articles':rows,'pair_errors':pair_errors}


def run(cache, plan, annotation, allow_rule_changes=False):
    required={'news_facts.py','news_review.py','np_scores.py','np_config.json'}
    if set(plan.get('rule_sha256',{}))!=required or not plan.get('cases'):
        return {'status':'invalid_freeze_manifest','cases':[]}
    matched={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()==digest for name,digest in plan['rule_sha256'].items()}
    result={'phase':'regression_after_rule_change' if allow_rule_changes else 'frozen_rules_holdout',
            'fact_rule_version':facts.VERSION,'frozen_rule_check':matched,'cases':[],
            'limits':['내용 검토 라벨은 별도 사람 검수 전의 보조 평가이며 독립 정답 데이터가 아님',
                      '네이버 수록 기사·수집된 본문·사건 창에 한정된 작은 편의 표본',
                      '180일 기준 미수집이므로 N/P나 수익률 예측력을 평가하지 않음']}
    if allow_rule_changes:
        result['limits'].append('이미 규칙 보완에 사용한 기사이므로 이 결과는 새 기사 정확도 평가가 아닌 회귀 확인임')
    if not all(matched.values()) and not allow_rule_changes:
        result['status']='rules_changed'
        return result
    entries=annotation.get('articles',[])
    if not isinstance(entries,list) or any(not isinstance(a,dict) for a in entries):
        result['status']='invalid_annotations'
        return result
    if len({a.get('url') for a in entries})!=len(entries) or any(
        not isinstance(a.get('url'),str) or not isinstance(a.get('has_primary_fact'),bool) or not isinstance(a.get('expected'),dict)
        or (a.get('has_primary_fact') and (not a.get('group') or not {'actor','action','target'}<=a['expected'].keys()))
        or not isinstance(a.get('accepted_values',{}),dict)
        or any(not isinstance(v,list) for v in a.get('accepted_values',{}).values())
        or not isinstance(a.get('expected_facts',[]),list)
        or any(not valid_fact_label(l) for l in a.get('expected_facts',[]))
        for a in entries):
        result['status']='invalid_annotations'
        return result
    label_map={a['url']:a for a in entries}
    for spec in plan['cases']:
        population,coverage=load_case(cache,spec)
        unlabelled=[a['url'] for a in population if a['url'] not in label_map]
        stale=[a['url'] for a in population if a['url'] in label_map and label_map[a['url']].get('article_sha256')!=article_hash(a)]
        overlap=[a['url'] for a in population if a['url'] in set(plan.get('preexisting_article_urls',[]))]
        case={'case_id':spec.get('case_id',spec.get('drop_id')),'drop_id':spec.get('drop_id'),
              'sample_type':spec.get('sample_type','drop_event'),'coverage':coverage,
              'unlabelled_articles':unlabelled,'stale_labels':stale,'preexisting_articles':overlap}
        if not coverage['collection_complete']:
            case['status']='collection_incomplete'
        elif overlap and not allow_rule_changes:
            case['status']='preexisting_article_overlap'
        elif unlabelled or stale:
            case['status']='annotation_incomplete'
        else:
            case.update(status='evaluated',evaluation=evaluate(population,label_map))
        result['cases'].append(case)
    result['status']='evaluated' if all(c['status']=='evaluated' for c in result['cases']) else 'incomplete'
    evaluated=[c['evaluation'] for c in result['cases'] if c['status']=='evaluated']
    fact_count=sum(c['primary_fact_articles'] for c in evaluated)
    correct=sum(c['correct_extractions'] for c in evaluated)
    result['summary']={'evaluated_cases':len(evaluated),'article_count':sum(c['article_count'] for c in evaluated),
                       'primary_fact_articles':fact_count,'correct_extractions':correct,
                       'multi_fact_articles':sum(c['multi_fact_articles'] for c in evaluated),
                       'fact_coverage':correct/fact_count if fact_count else None,
                       'held_fact_articles':sum(c['held_fact_articles'] for c in evaluated),
                       'field_error_articles':sum(c['field_error_articles'] for c in evaluated)}
    return result


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cache',type=Path,default=HERE/'cache/naver')
    ap.add_argument('--regression',action='store_true',help='이미 검토한 기사를 회귀 사례로 비교. 새 기사 평가와 구분')
    ap.add_argument('--annotations',type=Path,help='명시적으로 사용할 내용 검토 라벨 파일')
    ap.add_argument('--plan',type=Path,help='명시적으로 사용할 동결 계획 파일')
    ap.add_argument('--out',type=Path,help='별도로 저장할 평가 결과 파일')
    args=ap.parse_args(argv)
    directory=args.cache/'holdout'
    plan=json.loads((args.plan or directory/'plan_v1.json').read_text(encoding='utf-8'))
    annotations=args.annotations or directory/'annotations_v1.json'
    annotation=json.loads(annotations.read_text(encoding='utf-8')) if annotations.exists() else {'articles':[]}
    result=run(args.cache,plan,annotation,allow_rule_changes=args.regression)
    result['annotation_path']=str(annotations.resolve())
    if args.regression:
        out=directory/f'regression_{facts.VERSION}.json'
    elif result['status']!='evaluated':
        out=directory/'evaluation_attempt_v1.json'
    else:
        out=directory/'evaluation_v1.json'
    if args.out:
        out=args.out
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'status':result['status'],'cases':[{'case_id':c.get('case_id',c['drop_id']),'status':c['status'],
                      'article_count':c.get('evaluation',{}).get('article_count'),
                      'correct_extractions':c.get('evaluation',{}).get('correct_extractions'),
                      'held_fact_articles':c.get('evaluation',{}).get('held_fact_articles'),
                      'field_error_articles':c.get('evaluation',{}).get('field_error_articles')}
                     for c in result['cases']],'saved_to':str(out)},ensure_ascii=False,indent=2))
    return 0 if result['status']=='evaluated' else 1


if __name__=='__main__':
    raise SystemExit(main())
