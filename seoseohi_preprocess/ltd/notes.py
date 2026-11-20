# Vendored from work/kq120_2026_fill (2026-11-12 engine that produced the delivered LTD fills).
# Only I/O was changed (document path / statement totals are passed in); parsing rules are unchanged.
import re
from . import dartdoc as D
STMT=re.compile(r'부채총계|자산총계|영업활동|자본총계|기초자본|전기초|당기초|매출액|매출원가')
def note_tables(docs):
    out=[]
    for fn,x in docs:
        for t in D.tables(x):
            flat=' '.join(' '.join(r) for r in t['rows'])
            if STMT.search(flat): continue
            if not re.search(r'차입|사채|금융부채|부채',flat): continue
            u=D.FACT.get(t['unit'] or '원',None)
            if u is None: continue
            t['fn']=fn; t['u']=u; t['flat']=flat; out.append(t)
    return out
BOND=re.compile(r'(^|[^투자])(전환사채|신주인수권부사채|교환사채|일반사채|사모사채|공모사채|무보증사채|회사채|^사채)')
LTDL=re.compile(r'장기차입금|유동성장기|시설자금|장기차입부채')
def cells_numeric(t):
    for r in t['rows']:
        lab=' '.join(c for c in r if D.num(c) is None)
        for c in r:
            v=D.num(c)
            if v is not None: yield lab, v*t['u'], t
def find_value(nts, target, labre=None):
    hits=[]
    if not target: return hits
    for t in nts:
        tol=t['u'] if t['u']>1 else 1
        for lab,v,_ in cells_numeric(t):
            if abs(v-target)<=tol and (labre is None or re.search(labre,lab)):
                hits.append((t['fn'],t['idx'],lab[:60],v,t['unit']))
    return hits
def hidden_scan(nts):
    """return bond/ltd note rows with nonzero values (liability context only)"""
    b=[];l=[]
    for t in nts:
        if re.search(r'금융자산|투자|매도가능|당기손익-공정가치 측정 금융자산',t['ctx'][-150:]) and not re.search('부채',t['ctx'][-150:]): continue
        for r in t['rows']:
            lab=' '.join(c for c in r if D.num(c) is None)
            vals=[D.num(c) for c in r if D.num(c) is not None]
            if not vals or not any(abs(v)>0 for v in vals): continue
            if re.search(r'투자|자산|이자|비용|수익|손익|이익|손실|발행일|만기일|이자율|전환가액|행사가|주식수|행사|전환효과|보통주|이항|변동성|할인율|옵션|조기상환|안건|승인|가결|찬성|파생|공정가치|평가|상각|증가|감소|상환$|발행$|전환$|기초|기말|대체|현금흐름',lab): continue
            if max(abs(v) for v in vals)*t['u']<1e7: continue
            if BOND.search(lab.replace(' ','')): b.append((t['idx'],lab[:50],vals[:3],t['unit']))
            if LTDL.search(lab.replace(' ','')): l.append((t['idx'],lab[:50],vals[:3],t['unit']))
    return b,l

def bond_values(nts):
    """set of won values appearing in bond-labelled rows (carrying amounts)"""
    out=set()
    for t in nts:
        flat=t['flat']
        if not re.search(r'사채',flat): continue
        for r in t['rows']:
            lab=' '.join(c for c in r if D.num(c) is None).replace(' ','')
            if not re.search(r'^(유동성?|비유동)?(전환사채|신주인수권부사채|교환사채|사채|사모사채|공모사채)(합계|계|장부금액|장부가액)?$|^(사채|전환사채|신주인수권부사채)?장부(금액|가액)$',lab): continue
            if re.search(r'이익|손실|이자|비용|행사|발행금액|액면',lab): continue
            for c in r:
                v=D.num(c)
                if v and abs(v)*t['u']>=1e7: out.add((round(v*t['u']),t['u']))
    return out
def split_candidates(nts):
    S=[];L1=[];L2=[]
    for t in nts:
        for r in t['rows']:
            lab=' '.join(c for c in r if D.num(c) is None).replace(' ','')
            vals=[D.num(c) for c in r]; vals=[abs(v)*t['u'] for v in vals if v is not None and v!=0]
            if not vals: continue
            if re.search(r'유동성장기차입금|유동성장기부채|1년이내.*차입|유동성장기차입|장기차입금.*유동성|유동성대체',lab): L1+= [(v,t['u']) for v in vals]
            elif re.search(r'유동성사채|유동성전환사채|유동성신주인수권부사채|사채.*유동성',lab): L2+= [(v,t['u']) for v in vals]
            elif re.search(r'단기차입금|단기차입부채',lab) and not re.search('유동성',lab): S+= [(v,t['u']) for v in vals]
    return S,L1,L2
