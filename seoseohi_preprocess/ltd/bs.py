# Vendored from work/kq120_2026_fill (2026-11-12 engine that produced the delivered LTD fills).
# Only I/O was changed (document path / statement totals are passed in); parsing rules are unchanged.
import json,re,os
from . import dartdoc as D
def _norm(s): return re.sub(r'\s','',s)
def bs_table(rcpt, fs_div, docs, totals):
    """docs: list of (filename, xml) from the receipt's document.zip.
    totals: reported BS totals of THIS receipt/basis (Liabilities, Assets,
    CurrentLiabilities, Equity) used to pick the right statement table."""
    cand=[]
    targets=set(round(v) for v in totals if v is not None)
    best=None
    for fn,x in docs:
        main = fn.endswith(rcpt+'.xml') or len(docs)==1
        for t in D.tables(x):
            labs=_norm(' '.join(r[0] for r in t['rows'] if r))
            if '부채총계' not in labs or '유동부채' not in labs: continue
            ctx=_norm(t['ctx'][-250:])
            isc = '연결재무상태표' in ctx or '연결대차대조표' in ctx
            if (fs_div=='CFS') != isc: continue
            if '요약' in ctx[-120:]: continue
            uf=D.FACT.get(t['unit'] or '원',1)
            vals=set()
            for r in t['rows']:
                for c in r[1:]:
                    v=D.num(c)
                    if v: vals.add(round(v*uf))
            hit=len(targets & vals)
            if targets and not hit: continue
            sc=(min(hit,4), 0 if main else 1, len(t['rows']))
            if best is None or sc>best[3]: best=(hit,fn,t,sc,uf)
    if not best: return None
    if not targets and fs_div=='OFS':
        pass
    return dict(doc=best[1],table=best[2],uf=best[4],docs=docs,facts=cand,hit=best[0])
def period_cols(rows):
    """return list of column-index groups per period, from header rows"""
    for r in rows[:3]:
        hdr=[_norm(c) for c in r]
        groups=[]; seen=[]
        for i,h in enumerate(hdr[1:],1):
            if re.search(r'제\d+|당기|전기|\d{4}', h):
                if h not in seen: seen.append(h); groups.append([i])
                else: groups[seen.index(h)].append(i)
        if len(groups)>=2: return groups
    return None
NONDEBT=re.compile(r'리스|파생|상환전환우선주|보증|미지급|충당|이연|선수|예수|매입|퇴직|확정급여|법인세|임대보증|장기종업원|복구|정부보조|계약부채|금융보증|우선주부채|기타')
def classify(label, section):
    l=re.sub(r'\(주[\d,\s]*\)','',re.sub(r'^(([ⅠⅡⅢⅣⅤⅥ]+|[IVX]+(?=\.)|[0-9]+)\.?|[가-하]\.|\(\d+\))','',re.sub(r'\s','',label)))
    l=re.sub(r'\([^)]*\)$','',l) if not re.search(r'차입|사채',l.split('(')[-1]) else l
    if re.fullmatch(r'(단기|유동)?금융(채무|부채)',l) and section=='cur': return 'amb:'+l
    if re.fullmatch(r'(장기|비유동)금융(채무|부채)',l) and section=='nc': return 'ambnc:'+l
    if not re.search(r'차입|사채|유동성장기부채|^장기부채$|유동성장기차입',l): return None
    if re.search(r'리스|파생|상환전환우선주부채|금융보증|미지급|상환할증금$|전환권조정|사채할인',l): return 'excl:'+l
    if l in ('기타금융부채','기타차입금'): return 'amb:'+l
    if section=='cur':
        if re.fullmatch(r'단기차입금|단기차입부채|단기사채|전자단기사채|단기금융부채',l): return 'short'
        if re.search(r'유동성|1년이내|당기상환',l) and not re.search(r'단기',l): return 'ltd_cur'
        if re.fullmatch(r'(유동)?(전환사채|신주인수권부사채|교환사채|상환사채|전환사채및신주인수권부사채)',l): return 'ltd_cur'
        if re.fullmatch(r'(유동)?사채',l): return 'ltd_cur_plain'
        if re.search(r'단기.*(및|와|,|ㆍ|·).*유동성|차입금$|차입부채$|유동금융부채',l): return 'amb:'+l
        return 'amb:'+l
    else:
        if re.fullmatch(r'(비유동)?(장기)?(차입금|차입부채|사채|전환사채|신주인수권부사채|교환사채|장기차입금|장기사채)(및사채|및차입금)?',l) or re.search(r'장기차입|사채$|차입금$|차입부채$',l): return 'ltd_nc'
        return 'amb:'+l
def parse_bs(t, uf):
    rows=t['rows']
    groups=period_cols(rows)
    out=[]; sec=None; inliab=False
    for r in rows:
        if not r: continue
        lab=r[0]
        if groups:
            vals=[]
            for g in groups:
                v=None
                for i in g:
                    if i<len(r):
                        x=D.num(r[i])
                        if x is not None and r[i].strip()!='': v=x; break
                vals.append(v)
        else:
            vals=[D.num(c) for c in r[1:] if c.strip()!='' and D.num(c) is not None and not re.fullmatch(r'[\d,\s]{1,8}',c) or False] if False else [D.num(c) for c in r[1:]]
        ll=re.sub(r'^(([ⅠⅡⅢⅣⅤⅥ]+|[IVX]+(?=\.)|[0-9]+)\.?|[가-하]\.|\(\d+\))','',re.sub(r'\s','',lab))
        if re.fullmatch(r'부채',ll): inliab=True; continue
        if re.match(r'유동부채',ll): sec='cur'; inliab=True; out.append(('TOTAL_CUR',lab,vals)); continue
        if re.match(r'비유동부채',ll): sec='nc'; out.append(('TOTAL_NC',lab,vals)); continue
        if re.match(r'부채총계|부채합계',ll): out.append(('TOTAL_L',lab,vals)); break
        if inliab and sec: out.append((sec,lab,vals))
    return out
def hier_check(items, total):
    """items list of values (None->0). returns True if some parent/child grouping sums to total"""
    vals=[v or 0 for v in items]
    if total is None: return False
    if abs(sum(vals)-total)<1: return True
    # greedy: treat a row as parent if equal to sum of next k rows
    i=0; top=0; n=len(vals)
    while i<n:
        v=vals[i]; s=0; skip=0
        for k in range(i+1,n):
            s+=vals[k]
            if abs(s-v)<1 and k>i: skip=k-i; break
        top+=v; i+=1+skip
    return abs(top-total)<1
