# Vendored from work/kq120_2026_fill (2026-11-12 engine that produced the delivered LTD fills).
# Only I/O was changed (document path / statement totals are passed in); parsing rules are unchanged.
import re
from . import dartdoc as D, bs as B, notes as N
def d_split(amb, S, L1, L2):
    tol_max=1000
    sols=set()
    Sv=[(0,1)]+S; L1v=[(0,1)]+L1; L2v=[(0,1)]+L2
    for s,us in Sv:
        for a,ua in L1v:
            for b,ub in L2v:
                tol=max(us,ua,ub)
                tol = 1 if tol==1 else tol*1.5
                if abs(s+a+b-amb)<=tol: sols.add((round(a+b), tol>1))
    vals=set(v for v,_ in sols)
    if len(vals)==1:
        v=vals.pop(); approx=any(ap for vv,ap in sols if vv==v); return v,approx
    return None,None
def analyze(rcpt, fs_div, docs, totals):
    res=dict(rcpt=rcpt,fs_div=fs_div)
    try: r=B.bs_table(rcpt, fs_div, docs, totals)
    except Exception as ex:
        res['status']='DOC_CORRUPT' if 'zip' in str(ex).lower() else 'ERR'; res['err']=str(ex); return res
    if r is None: res['status']='NO_BS'; return res
    uf=r['uf']; p=B.parse_bs(r['table'],uf)
    res['bs_doc']=r['doc']; res['bs_table']=r['table']['idx']; res['bs_unit']=r['table']['unit']
    tot={}
    for x in p:
        if x[0].startswith('TOTAL') and any(v is not None for v in x[2]): tot[x[0]]=x[2]
    nts=N.note_tables(r['docs'])
    hb,hl=N.hidden_scan(nts)
    BV=N.bond_values(nts)
    S,L1,L2=N.split_candidates(nts)
    out={}
    hrows=set()
    for sec,lab,vals in p:
        if sec not in ('cur','nc') or B.classify(lab,sec): continue
        if not re.search(r'금융부채|금융채무',lab) or re.search(r'파생|공정가치|당기손익|리스',lab): continue
        for v in vals[:2]:
            if v and any(abs(v*uf-bv)<=(u if u>1 else 1) for bv,u in BV): hrows.add((sec,lab))
    for pi,per in enumerate(['cur','prior']):
        rows=[]
        for sec,lab,vals in p:
            if sec not in ('cur','nc'): continue
            v=vals[pi] if pi<len(vals) else None
            v=(v or 0)*uf
            rows.append([sec,lab,v,B.classify(lab,sec)])
        has_liq_bond=any(x[3]=='ltd_cur' and re.search('사채',x[1]) for x in rows)
        for x in rows:
            if x[3]=='ltd_cur_plain': x[3]='amb:'+x[1] if has_liq_bond else 'ltd_cur'
        debt=[x for x in rows if x[3] and not x[3].startswith('excl') and x[2]!=0]
        S_all=sum(x[2] for x in debt)
        def sec_ok(sec,key):
            t=tot.get(key)
            if not t or pi>=len(t) or t[pi] is None: return False
            return B.hier_check([x[2]/uf for x in rows if x[0]==sec], t[pi])
        rec=sec_ok('cur','TOTAL_CUR') and sec_ok('nc','TOTAL_NC')
        A=N.find_value(nts,S_all,r'계|합|차입|사채|부채') if S_all else []
        flags=[]; parts=[]
        ltd=0; unresolved=[]
        for x in debt:
            if x[3] in ('ltd_cur','ltd_nc'): ltd+=x[2]; parts.append((x[1],x[2],'포함'))
            elif x[3]=='short': parts.append((x[1],x[2],'제외(단기)'))
            elif x[3].startswith('ambnc'):
                ltd+=x[2]; parts.append((x[1],x[2],'포함(비유동 금융채무: 주석 합계로 확인 필요)')); flags.append('NC_FIN_LIAB_ROW')
            elif x[3].startswith('amb'):
                if x[0]=='cur':
                    v,ap=d_split(x[2],S,L1,L2)
                    if v is None:
                        nc_ltd=sum(y[2] for y in debt if y[3]=='ltd_nc')
                        c2=set()
                        for l,u in L1+L2:
                            if 0<l<=x[2]+1 and N.find_value(nts,l+nc_ltd,r'계|합'): c2.add(round(l))
                        if len(c2)==1: v=c2.pop(); ap=True; flags.append('D2_NOTE_LTD_TOTAL_MINUS_NONCURRENT')
                    if v is not None:
                        ltd+=v; parts.append((x[1],x[2],f'주석으로 분리: 유동성장기분 {v}')); flags.append('D_NOTE_SPLIT'+('_천원근사' if ap else ''))
                    else:
                        parts.append((x[1],x[2],'제외(PM 규칙: 묶인 유동 차입금)')); flags.append('P_PM_NONCURRENT_ONLY')
                else:
                    unresolved.append(x[1])
        has_bond_bs=any(re.search('사채',x[1]) for x in debt)
        has_ltd_bs=any(re.search('장기|유동성',x[1]) for x in debt) or any(x[0]=='nc' for x in debt)
        hidden=[]
        hb_p=[b for b in hb]  # period-agnostic
        if hb_p and not has_bond_bs:
            # H: bond value equals some other BS liability row
            hr=[x for x in rows if (x[0],x[1]) in hrows]
            if hr:
                for x in hr:
                    ltd+=x[2]; parts.append((x[1],x[2],'포함(주석 사채 장부금액과 일치하는 줄)'))
                flags.append('H_BOND_IN_OTHER_LINE')
            elif not A: hidden.append('note_bond_not_in_bs')
        if hl and not has_ltd_bs and not A: hidden.append('note_ltd_not_in_bs')
        A2=[]
        if not A and ltd:
            A2=N.find_value(nts,ltd,r'계|합|장기|사채|차입')
            if A2 and 'note_bond_not_in_bs' in hidden:
                tb=[nt for nt in nts if nt['idx']==A2[0][1] and nt['fn']==A2[0][0]]
                if not (tb and re.search('사채',tb[0]['flat'])): A2=[]
            if A2: hidden=[]
        if 'NC_FIN_LIAB_ROW' in flags and not (A or A2): unresolved.append('비유동 금융채무 구성 미확인')
        d=dict(S_all=S_all,A2=A2[:2],rows=[tuple(x) for x in debt],parts=parts,rec=rec,A=A[:2],hidden=hidden,flags=flags)
        if unresolved: d.update(rule=None,value=None,why='AMBIGUOUS_NONCURRENT_ROW '+','.join(unresolved))
        elif A: d.update(rule='A_NOTE_TOTAL_MATCH',value=ltd)
        elif A2: d.update(rule='A2_NOTE_LTD_TOTAL_MATCH',value=ltd)
        elif rec and not hidden: d.update(rule='B_BS_DIRECT_RECONCILED' if ltd else 'Z_ZERO_NO_LTD_ROWS',value=ltd)
        else: d.update(rule=None,value=None,why=('BS_NOT_RECONCILED ' if not rec else '')+' '.join(hidden))
        out[per]=d
    res['per']=out; res['note_bond_rows']=hb[:5]; res['note_ltd_rows']=hl[:5]; res['status']='OK'
    return res
