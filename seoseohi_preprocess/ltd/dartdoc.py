# Vendored from work/kq120_2026_fill (2026-11-12 engine that produced the delivered LTD fills).
# Only I/O was changed (document path / statement totals are passed in); parsing rules are unchanged.
import zipfile, re, os, json, html
TAG=re.compile(r'<[^>]+>')
def _txt(s):
    s=TAG.sub(' ',s); s=html.unescape(s); return re.sub(r'\s+',' ',s).strip()
def read_xml(b):
    for enc in ('utf-8','euc-kr','cp949'):
        try: return b.decode(enc)
        except Exception: pass
    return b.decode('utf-8','ignore')
def load(zip_path):
    z=zipfile.ZipFile(zip_path); out=[]
    for i in z.infolist():
        out.append((i.filename, read_xml(z.read(i))))
    return out
TBL=re.compile(r'<TABLE\b.*?</TABLE>',re.S|re.I)
TR=re.compile(r'<TR\b.*?</TR>',re.S|re.I)
TC=re.compile(r'<(T[DHEU])\b([^>]*)>(.*?)</\1>',re.S|re.I)
def tables(x):
    """yield dict(idx, context(text before), rows[list of cell texts], unit)"""
    res=[]; last=0
    for k,m in enumerate(TBL.finditer(x)):
        ctx=_txt(x[max(0,m.start()-1500):m.start()])[-400:]
        rows=[]
        for r in TR.finditer(m.group(0)):
            cells=[]
            for c in TC.finditer(r.group(0)):
                cs=re.search(r'COLSPAN="?(\d+)',c.group(2),re.I)
                t=_txt(c.group(3)); cells.append(t)
                if cs:
                    for _ in range(int(cs.group(1))-1): cells.append(t)
            rows.append(cells)
        body=' '.join(' '.join(r) for r in rows[:3])
        unit=None
        for src in (body, ctx[-200:]):
            mm=re.search(r'단위\s*[:：]?\s*(천원|백만원|원|USD|달러|위안|RMB|CNY|천위안)',src)
            if mm: unit=mm.group(1); break
        res.append(dict(idx=k,ctx=ctx,rows=rows,unit=unit,pos=m.start()))
    return res
NUM=re.compile(r'^\(?-?[\d,]+(\.\d+)?\)?$')
def num(s):
    s=s.replace(' ','').replace('△','-').replace('Δ','-')
    if s in ('-','–','—','0'): return 0
    if not NUM.match(s): return None
    neg=s.startswith('(') or s.startswith('-')
    if not s.strip('()-').replace(',',''): return None
    v=float(s.strip('()-').replace(',',''))
    return -v if neg else v
FACT={'원':1,'천원':1000,'백만원':1000000}
