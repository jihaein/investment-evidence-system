"""기사에 명시된 거래·행동 후보 추출. 지원하지 않는 표현은 추측하지 않는다.

기업명·행동·대상과 수치를 근거 문장에 연결한다. 사실 검증/의미 이해의 완성형이 아니다.
"""
import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal

VERSION = 'news-facts-8'
NAME = r'[가-힣A-Za-z0-9&·]+'
STOP_TARGETS = {'회사', '주식회사', '관계사', '관계회사', '자회사', '보통', '신주', '발행', '규모', '자사', '해당',
                '전문회사', '투자회사', '광업회사', '생산업체', '풍력발전업체'}
ACTION_RULES = [
    ('office_open', r'사무소.*개소|개소.*사무소'),
    ('mou', r'협약|MOU|양해각서'),
    ('equity_acquire', r'지분.*(?:인수|취득|양수)|주식.*(?:인수|취득|양수)|인수.*지분|인수(?:소식|\s|$)'),
    ('cb_issue', r'(?:전환\s*사채|CB).*발행|발행.*(?:전환\s*사채|CB)'),
    ('equity_issue', r'유상\s*증자|유증'),
    ('supply_contract', r'수주|공급|납품'),
]

# 큰 단위는 조→억→만→원 순서다. 작은 단위와 마지막 원 단위까지 한 금액으로 잡는다.
MONEY_NUMBER = r'\d[\d,]*(?:\.\d+)?(?:\s*[천백십]\s*(?:\d[\d,]*(?:\.\d+)?)?)*'
MONEY_KRW = (r'(?<![\d.,천백십조억만])(?=\d)'
             rf'(?:{MONEY_NUMBER}\s*조\s*)?(?:{MONEY_NUMBER}\s*억\s*)?'
             rf'(?:{MONEY_NUMBER}\s*만\s*)?(?:{MONEY_NUMBER}\s*)?원')
FOREIGN_CURRENCY = r'달러|USD|유로|EUR|위안|CNY|엔화|JPY|파운드|GBP|\d[\d.,]*\s*(?:억|만)?엔(?:\s|\)|$)'


def money_resolution(raw):
    parts=list(re.finditer(r'(\d[\d,]*(?:\.\d+)?)\s*(조|억|만|천|백|십)?\s*(조|억|만)?',raw))
    last=parts[-1]
    decimals=len(last[1].split('.')[1]) if '.' in last[1] else 0
    units={'조':10**12,'억':10**8,'만':10**4,'천':1000,'백':100,'십':10,None:1}
    return str(Decimal(units[last[2]]*units[last[3]])/(10**decimals))


def contract_money(title,body):
    # 금액이 명시된 계약 항목을 우선하고 배경 매출액·시장 규모를 끌어오지 않는다.
    label=re.search(r'계약\s*(?:금액|규모)(?:은|는)?\s*[:：=]?\s*(.{0,65})',body)
    if label:
        if re.search(FOREIGN_CURRENCY,label[1],re.I):
            return None,'unsupported_contract_currency'
        match=re.match(r'(?:약|총|한화)?\s*('+MONEY_KRW+r')',label[1])
        if match:
            return match[1],None
    for clause in re.split(r'(?<=[.!?])\s+',body[:1500]):
        if re.search(r'공급\s*계약|수주\s*계약|납품\s*계약|계약(?:을|을\s*)?\s*(?:체결|맺)',clause):
            if re.search(FOREIGN_CURRENCY,clause,re.I):
                return None,'unsupported_contract_currency'
            match=re.search(MONEY_KRW,clause)
            if match:
                return match.group(),None
    match=re.search(MONEY_KRW,title)
    return (match.group(),None) if match else (None,'contract_amount_not_identified')


def unnamed_contract_party(value):
    """일반적인 역할·익명 표기를 법인명으로 채우지 않는다. 회사명 접미사는 자르지 않는다."""
    text=re.sub(r'\(주\)|주식회사|\s+','',value).strip(' \'"“”‘’')
    return bool(re.fullmatch(
        r'(?:(?:미국|캐나다|중국|일본|대한민국|한국|국내|해외|현지|해당|한|어느|대형|중소|글로벌)(?:의)?)*'
        r'(?:업체|회사|기업|제조업체|생산업체|공급업체|발주사|발주처|고객사|거래처|관계사|상대방|미공개|비공개|미정|공개불가)',text))


def contract_party(body):
    # 항목 경계를 먼저 찾는다. 영문 법인명의 공백·마침표·쉼표는 이름의 일부다.
    explicit=re.search(r'계약\s*상대(?:방)?\s*(?:[은는]\s*|[:：=]\s*)(.+?)'
                       r'(?=\s*-\.\s*|\n|(?:이며|이고|이다|입니다)(?:[.,\s]|$)|\.\s+(?=[가-힣])|$)',body)
    if explicit:
        value=re.sub(r'^(?:\(주\)|주식회사)\s*','',explicit[1].strip(' \'"“”‘’'))
        value=re.sub(r'^(?:미국|캐나다|중국|일본|대한민국)\s+','',value)
        if unnamed_contract_party(value):
            return None,None
        if value and value not in {'미국','캐나다','중국','일본','대한민국','해외','국내'}:
            return value,explicit.group()
    party=r'(?:[A-Za-z][A-Za-z0-9&.,\- ]*?|'+NAME+r')'
    pattern=(r'(?<![가-힣A-Za-z0-9])(?:\(주\)|주식회사)?\s*[\'"“‘]?('
             +party+r')(?:\([^()]+\))?[\'"”’]?(?:과|와)(?:도)?\s')
    for match in re.finditer(pattern,body[:900]):
        value=match[1].strip()
        if unnamed_contract_party(value):
            continue
        if value in {'저장','수송','제작','공급','납품','계약','가공','기존','공법','판매',
                     '미국','캐나다','중국','일본','대한민국','해외','국내','이','그','이것','함께'}:
            continue
        after=re.split(r'(?<=[.!?])\s+',body[match.end():match.end()+130])[0]
        if re.search(r'공급.{0,8}계약|납품.{0,8}계약|계약.{0,12}(?:체결|맺)|^(?:맺|체결)',after):
            return value,match.group()
    return None,None


def contract_region(body):
    """공급 항목 또는 공급 목적지 문맥만 사용한다. 회사 소재 국가는 근거가 아니다."""
    explicit=re.search(r'(?:판매\s*(?:및|[·ㆍ/])\s*)?공급\s*지역\s*(?:[은는]|[:：])\s*'
                       r'(.+?)(?=\s*-\.\s*|\n|[.。]|$)',body)
    if explicit:
        value=re.split(r'(?:등)?(?:으로|에서)|(?:이며|이고|이다|입니다)',explicit[1],maxsplit=1)[0].strip()
        value=re.sub(r'\s+및\s+',', ',value)
        if value:
            return value,explicit.group()
    place=r'(?:캐나다|미국|중국|일본|베트남|대한민국|이란|미주|북미|남미|아시아|유럽|중동|국내|해외)'
    places=place+r'(?:\s*(?:,|·|및|와|과)\s*'+place+r')*'
    for match in re.finditer(r'(?<![가-힣])('+places+r')\s*(?:각\s*)?(?:공장|시장|지역|원전|에|로)',body[:1500]):
        tail=body[match.end():match.end()+100]
        # 다음 문장까지 넘어가 소재지·배경을 공급지로 채택하지 않는다.
        tail=re.split(r'(?<=[.!?])\s+',tail)[0]
        supply=re.search(r'공급|납품',tail)
        if supply and not re.search(r'본사|소재|위치|기반|업체|회사|법인',tail[:supply.start()]):
            return match[1],match.group()+tail
    modifier=re.search(r'(?<![가-힣])('+place+r')\s+([가-힣]{2,20})\s*(?:공급|납품)\s*계약',body[:900])
    if modifier and not re.search(r'회사|법인|업체',modifier[2]):
        return modifier[1],modifier.group()
    return None,None


def contract_product(title,body,actor,party):
    # 알려진 동의어만 정규화한다. 회사 이름에 종속된 매핑은 사용하지 않는다.
    match=re.search(r'핵폐기물\s*(?:컨테이너|처리\s*용기|저장\s*용기)',title+' '+body[:900])
    if match:
        return 'nuclear_waste_container',match.group()
    label=re.search(r'계약명\s*[:：]\s*(.+?)(?=\s*-\.|[\n.]|$)',body)
    text=label[1] if label else normalize_title(title)
    if not label:
        # 제목의 상대방 설명·금액을 공급물로 남기지 않는다. 본문에 명시된 공급물을 우선한다.
        product=re.search(r'지역을\s*대상으로\s+([^.!?\n]{2,45}?)(?:을|를)\s*공급',body[:900])
        if not product:
            product=re.search(r'(?:와|과)\s+([가-힣A-Za-z0-9 ]{2,35}?)(?:을|를)\s+[가-힣 ]{1,20}에\s*공급',body[:900])
        if not product:
            product=re.search(r"규모(?:의)?\s+(?:['\"‘“])?([^.!?\n]{2,65}?)\s*공급\s*계약",body[:900])
        if not product:
            product=re.search(r"(?:와|과)\s+(?!\d)(?:['\"‘“])?([^.!?\n]{2,65}?)\s*공급\s*계약",body[:900])
        if product:
            candidate=product[1].strip(' \'"‘’“”')
            if not re.search(MONEY_KRW,candidate):
                text=candidate
    text=text.replace(actor,'',1).split('…')[0]
    if party:
        text=re.sub(re.escape(party)+r'(?:과|와)?','',text)
    text=re.sub(r'\([0-9]{6}\)','',text)
    text=re.sub(MONEY_KRW,'',text)
    for money in amounts(text):
        text=text.replace(money,'')
    text=re.sub(r'캐나다|미국|중국|일본|베트남|대한민국|국내|해외|유럽|이란|미주|아시아','',text)
    text=re.sub(r'(?:체결)?\s*(?:의\s*)?건\s*$','',text)
    text=re.sub(r'단일판매|판매|수주공시|수주|공급|납품|계약|체결|규모|결정|(?:^|\s)에\s','',text)
    text=re.sub(r'[^가-힣A-Za-z0-9]','',text)
    if len(text)<2 or text in {'장비','제품','설비','물품','취소','해지','수주공시'}:
        return None,None
    return text,label[1] if label else (product.group() if product else title)


def contract_period(body,published):
    match=re.search(r'계약(?:의)?\s*(?:기간|종료일)(?:은|는)?\s*[:：]?\s*(.{0,170})',body)
    if not match:
        return {},{}
    text=match[1]
    text=re.split(r'까지|\s*-\.|다\.\s',text,maxsplit=1)[0]
    dates=[]
    pattern=(r'(?<![\d./-])(?:(?:(\d{4})\s*년|(내년|올해|금년))\s*)?'
             r'(\d{1,2})\s*월\s*(\d{1,2})\s*일'
             r'|(\d{4})[./-](\d{1,2})[./-](\d{1,2})|지난\s*(\d{1,2})\s*일')
    for item in re.finditer(pattern,text):
        if item[8]:
            anchor=published.replace(day=int(item[8]))
            if anchor>published:
                previous=published.replace(day=1)-timedelta(days=1)
                anchor=previous.replace(day=int(item[8]))
        else:
            year=int(item[1] or item[5] or (published.year+1 if item[2]=='내년' else published.year))
            month,day=int(item[3] or item[6]),int(item[4] or item[7])
            anchor=published.replace(year=year,month=month,day=day)
            if not (item[1] or item[2] or item[5]) and anchor.date()<published.date():
                # 과거 월일이 지난 계약인지 내년 종료인지 문장만으로 정할 수 없다.
                raise ValueError('ambiguous contract year')
        dates.append((anchor.date().isoformat(),item.group()))
    if len(dates)>2:
        raise ValueError('multiple contract date ranges')
    fields,evidence={},{}
    if len(dates)>=2:
        fields.update(contract_start=dates[0][0],contract_end=dates[1][0])
        evidence.update(contract_start=dates[0][1],contract_end=dates[1][1])
    elif dates:
        fields['contract_end'],evidence['contract_end']=dates[0]
    if '이날부터' in text and 'contract_start' not in fields:
        announced=re.search(r'(\d{1,2})일.{0,100}(?:공시|밝혔)',body[:650])
        if announced:
            anchor=published.replace(day=int(announced[1]))
            if anchor>published:
                previous=published.replace(day=1)-timedelta(days=1)
                anchor=previous.replace(day=int(announced[1]))
            fields['contract_start']=anchor.date().isoformat()
            evidence['contract_start']='이날부터 / '+announced.group()
    if fields.get('contract_start','')>fields.get('contract_end','9999'):
        raise ValueError('contract end before start')
    return fields,evidence


def extract_contract(article,actor,result):
    title,body=article['title'],article['body']
    confirmed=re.search(r'공급.{0,8}계약|수주\s*계약|납품.{0,8}계약|판매[ㆍ·/\s]*공급계약',body[:1500])
    if not confirmed and not ('공급' in title and re.search(r'계약\s*기간',body[:900]) and re.search(r'계약\s*금액',body[:900])):
        result['reason']='contract_not_confirmed_in_body'
        return result
    if re.search(r'정정|금액\s*변경|기간\s*연장',title):
        result['reason']='unsupported_contract_update'
        return result
    money,error=contract_money(title,body)
    if error:
        result['reason']=error
        return result
    party,party_evidence=contract_party(body)
    target,target_evidence=contract_product(title,body,actor,party)
    if not target:
        result['reason']='contract_product_not_identified'
        return result
    fields={'amount_krw':number(money),'currency':'KRW'}
    evidence={'actor':title,'action':title,'target':target_evidence,'amount_krw':money,'currency':money}
    if party:
        fields['counterparty'],evidence['counterparty']=party,party_evidence
    region,region_evidence=contract_region(body)
    if region:
        fields['delivery_region']=region
        evidence['delivery_region']=region_evidence
    try:
        period,period_evidence=contract_period(body,datetime.fromisoformat(article['published_at']))
    except ValueError:
        result['reason']='invalid_contract_period'
        return result
    fields.update(period);evidence.update(period_evidence)
    if not any(k in fields for k in ('counterparty','contract_end','delivery_region')):
        result['reason']='contract_identity_not_identified'
        return result
    stage='cancelled' if re.search(r'취소|해지',title) else ('completed' if re.search(r'납품\s*완료|공급\s*완료',title) else 'announced')
    fact={'actor':actor,'action':'supply_contract','target':target,'stage':stage,'fields':fields,
          'evidence':evidence,'amount_resolution_krw':money_resolution(money),
          'announcement_date':article['published_at'][:10]}
    # 비교용 근거는 원래 추출 필드와 분리한다. 법인명 동의어는 같은 원문에 병기된 것만 쓴다.
    aliases=[party] if party else []
    if party_evidence:
        bilingual=re.search(r'([가-힣A-Za-z0-9]+)\(([^()]+)\)',party_evidence)
        if bilingual and re.search(r'[A-Za-z]{2}',bilingual[2]):
            aliases.extend([bilingual[1],bilingual[2]])
    support=target_evidence or ''
    existing=re.search(r'기존.{0,180}?(?:공급|생산|$)',body)
    if existing:
        support+=' '+existing.group()
    terms=sorted({t.lower() for t in re.findall(r'[A-Za-z][A-Za-z0-9-]{2,}',support)})
    terms=[t for t in terms if t not in {'inc','ltd','corp','sarl','company','contract','supply'}]
    fact['comparison_evidence']={'counterparty_aliases':aliases,'product_terms':terms,
                                 'product_support':support}
    result.update(status='extracted_candidate',primary=fact,reason=None)
    return result


def normalize_title(title):
    return re.sub(r'^\s*(?:\[[^\]]+\]|<[^>]+>)\s*', '', title).strip(' "“”\'')


def number(text):
    """7천800, 150만, 34억1250만 등의 한국어 숫자를 Decimal로 정규화."""
    text = re.sub(r'[,\s원주%]', '', text)
    total, subtotal, pending = Decimal(0), Decimal(0), Decimal(0)
    for token in re.findall(r'\d+(?:\.\d+)?|[조억만천백십]', text):
        if token[0].isdigit():
            pending = Decimal(token)
        elif token in '조억만':
            unit = {'조': Decimal(10)**12, '억': Decimal(10)**8, '만': Decimal(10)**4}[token]
            total += (subtotal+pending or Decimal(1))*unit
            subtotal, pending = Decimal(0), Decimal(0)
        else:
            subtotal += (pending or Decimal(1))*{'천':1000, '백':100, '십':10}[token]
            pending = Decimal(0)
    value=format(total+subtotal+pending,'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def amounts(text):
    pattern = r'\d[\d,]*(?:\.\d+)?\s*(?:조|억)(?:\s*\d[\d,]*(?:\.\d+)?\s*억)?(?:\s*\d[\d,]*(?:\.\d+)?\s*만)?\s*원?'
    return [m.group().strip() for m in re.finditer(pattern, text)]


def entity_actor(title):
    match = re.match(NAME, normalize_title(title))
    return match.group() if match else None


def target_equity(text):
    matches = re.finditer(r'(' + NAME + r')(?:의)?\s*(?:주식|지분)', text)
    for match in matches:
        value = match[1]
        if value.endswith('의'):
            value = value[:-1]
        if value not in STOP_TARGETS and not value.isdigit():
            return value, match.group()
    return None, None


def primary_amount(title, body, action):
    body = re.sub(r'^(?:\s*\[[^\]]+\]\s*)+', '', body)
    direct = amounts(title)
    if direct:
        return direct[0]
    pattern = {'cb_issue': r'(?:전환\s*사채|CB).*발행|발행.*(?:전환\s*사채|CB)',
               'equity_issue': r'유상\s*증자|유증',
               'equity_acquire': r'취득|양수|인수'}[action]
    for clause in re.split(r'(?<=[.!?])\s+', body[:1500]):
        if re.search(pattern, clause, re.I) and amounts(clause):
            return amounts(clause)[0]
    return None


def action_of(title):
    for action, pattern in ACTION_RULES:
        if re.search(pattern, title, re.I):
            return action
    return None


def split_contracts(article,actor,result):
    """계약별 상대·금액이 연결된 문단만 분리한다. 각각/합계의 배분은 추측하지 않는다."""
    title,body=article['title'],article['body']
    spans=[]
    for clause in re.split(r'(?<=다)\.\s*',body):
        party,_=contract_party(clause)
        if party and re.search(r'(?:공급|납품).{0,8}계약',clause):
            spans.append([party,clause])
        elif spans:
            spans[-1][1]+='. '+clause
    stated_count=re.search(r'계약.{0,25}([2-9])\s*건',title+' '+body[:700])
    multiple=bool(stated_count or re.search(r'각각.{0,25}(?:억|만).*공급',title+' '+body[:700])) or len({p for p,_ in spans})>1
    if not multiple:
        return None
    result['reason']='multiple_contracts_not_safely_separable'
    if len(spans)<2 or len({p for p,_ in spans})!=len(spans):
        return result
    if stated_count and len(spans)!=int(stated_count[1]):
        return result
    outputs=[]
    for party,span in spans:
        # 계약별 원문에 금액이 여러 개면 합계/매출액과의 대응을 확정하지 않는다.
        monetary=re.findall(MONEY_KRW,re.split(r'최근\s*매출|매출액',span)[0])
        if len(monetary)!=1:
            return result
        local={**article,'body':span}
        parsed=extract_contract(local,actor,{'version':VERSION,'status':'needs_review','primary':None,'reason':None,'context':[]})
        if not parsed['primary'] or parsed['primary']['fields'].get('counterparty')!=party:
            return result
        parsed['primary']['source_excerpt']=span
        outputs.append(parsed['primary'])
    result.update(status='extracted_multiple_candidates',facts=outputs,reason=None)
    return result


def fact_list(extracted):
    return extracted.get('facts') or ([extracted['primary']] if extracted['primary'] else [])


def extract(article):
    """제목의 핵심 행동만 추출하고 본문의 다른 행동을 주 거래로 끌어오지 않는다."""
    title, body = article.get('title', ''), article.get('body', '')
    action = action_of(title)
    if not action and '투자' in title and re.search(r'지분.*(?:인수|취득)|주식.*취득', body[:400]):
        action = 'equity_acquire'
    actor = entity_actor(title)
    result = {'version': VERSION, 'status': 'needs_review', 'primary': None,
              'reason': 'unsupported_title_action', 'context': []}
    if not action or not actor:
        return result
    if action == 'equity_issue' and re.search(r'청약률|실권주|발행가액|발행가\s*확정|청약\s*결과', title):
        result['reason'] = 'unsupported_transaction_update'
        return result
    if re.search(r'사실무근|부인|검토.{0,8}않|발행.{0,8}않|인수.{0,8}않|확정.{0,8}없|미확정', title+' '+body[:220]):
        result['reason'] = 'negated_or_unconfirmed_claim'
        return result
    if action=='supply_contract':
        multiple=split_contracts(article,actor,result)
        if multiple is not None:
            return multiple
        return extract_contract(article,actor,result)
    lead = body[:650]
    target, target_evidence = None, None
    fields, evidence = {}, {'actor': title, 'action': title}
    if action == 'office_open':
        org = re.search(r'관계사\s+(' + NAME + r'?)(?:[이가은는의]|\s)', lead)
        if org:
            actor = org[1]
            evidence['actor'] = org.group()
        else:
            org = re.search(r'(' + NAME + r'?)(?:은|는|이|가)\s*(?:지난\s*\d+일.*?)?(?:서울|음성)[\s가-힣]*사무소', lead)
            if org:
                actor = org[1]
                evidence['actor'] = org.group()
        place = re.search(r'(' + NAME + r'사무소)', title+' '+lead)
        target = place[1] if place else None
        target_evidence = place.group() if place else None
    elif action == 'equity_acquire':
        target, target_evidence = target_equity(title)
        if not target:
            target, target_evidence = target_equity(lead)
        if not target:
            purchase = re.search(r'(' + NAME + r'?)(?:을|를).{0,45}인수', lead)
            if purchase:
                target, target_evidence = purchase[1], purchase.group()
        share = re.search(r'(\d[\d,천백십만]*(?:\.\d+)?)\s*주(?:를|\(|\s|$)', lead)
        if share:
            fields['shares'] = number(share[1])
            evidence['shares'] = share.group()
        stake = re.search(r'지분(?:율|비율)?(?:은|는)?\s*(\d+(?:\.\d+)?)\s*%', title+' '+lead)
        if stake:
            fields['stake_pct'] = number(stake[1])
            evidence['stake_pct'] = stake.group()
    elif action == 'cb_issue':
        target = 'perpetual_cb' if re.search(r'영구', title+' '+lead) else 'convertible_bond'
        target_evidence = title if '영구' in title else lead
        party = re.search(r'(' + NAME + r'?)(?:\s+외\s*\d+인)?(?:을|를)\s*대상', lead)
        if party:
            fields['counterparty'] = party[1]
            evidence['counterparty'] = party.group()
    elif action == 'equity_issue':
        target, target_evidence = 'paid_in_equity', title
    elif action == 'mou':
        partner = re.search(r'(' + NAME + r'?)(?:과|와)\s', title+' '+lead)
        if partner:
            target, target_evidence = partner[1], partner.group()
        # 단순 협약이라는 말만으로 모든 협약을 같은 사실로 보지 않는다.
        if re.search(r'금\s*유통', title+' '+lead):
            fields['purpose'] = 'gold_distribution'
            evidence['purpose'] = '금 유통'
    if action in ('cb_issue', 'equity_acquire', 'equity_issue'):
        money = primary_amount(title, body, action)
        precise = primary_amount('', body, action)
        if money and precise and action == 'equity_acquire' and ('shares' in fields or 'stake_pct' in fields):
            headline_value, body_value = Decimal(number(money)), Decimal(number(precise))
            if headline_value % 100000000 == 0 and 0 <= body_value-headline_value < 50000000:
                money = precise
        if money:
            fields['amount_krw'] = number(money)
            evidence['amount_krw'] = money
    if not target:
        result['reason'] = 'target_not_identified'
        return result
    if action in ('cb_issue', 'equity_issue') and 'amount_krw' not in fields:
        result['reason'] = 'transaction_amount_not_identified'
        return result
    if action == 'equity_acquire' and not fields:
        result['reason'] = 'transaction_details_not_identified'
        return result
    stage = 'announced'
    for state, pattern in [('cancelled', r'철회|취소'), ('delayed', r'연기'), ('completed', r'완료|납입\s*끝'),
                           ('planned', r'개소\s*(?:할\s*)?예정|협약.*예정|인수.*검토|발행\s*(?:계획|검토)')]:
        if re.search(pattern, title):
            stage = state
            break
    fact = {'actor': actor, 'action': action, 'target': target, 'stage': stage,
            'fields': fields, 'evidence': {**evidence, 'target': target_evidence}}
    # 개소/협약은 금액을 갖지 않으므로 날짜 없는 장기 반복을 동일 사건으로 단정하지 않는다.
    if action == 'office_open':
        date = re.search(r'지난\s*(\d{1,2})일', lead)
        if date and article.get('published_at'):
            published = datetime.fromisoformat(article['published_at'])
            try:
                when = published.replace(day=int(date[1]))
                if when > published:
                    previous = published.replace(day=1)-timedelta(days=1)
                    when = previous.replace(day=int(date[1]))
                fields['event_date'] = when.date().isoformat()
                fact['evidence']['event_date'] = date.group()
            except ValueError:
                pass
    # 배경은 주 행동과 구분한 정형 주제 표시로만 제공하며 N의 새 사실 수에 더하지 않는다.
    if re.search(r'금광|금\s*유통', title+' '+body):
        result['context'].append('gold_mining_or_distribution')
    result.update(status='extracted_candidate', primary=fact, reason=None)
    return result


def core(fact):
    return tuple(fact[k] for k in ('actor', 'action', 'target', 'stage'))


def party_key(value):
    # 법인 형태 표기만 정리한다. 이름의 주요 부분과 공동 계약 상대의 &는 보존한다.
    value=re.sub(r'\(주\)|주식회사','',value).strip().lower()
    value=re.sub(r'(?:[.,\s]+(?:incorporated|inc|corporation|corp|company|co|limited|ltd|llc))+[.,\s]*$','',value)
    return re.sub(r'\s+',' ',value).strip(' .,')


def party_aliases(fact):
    values=fact.get('comparison_evidence',{}).get('counterparty_aliases',[])
    if fact['fields'].get('counterparty'):
        values=values+[fact['fields']['counterparty']]
    return {party_key(v) for v in values if v and party_key(v)}


def alias_context(facts):
    # 서로 다른 공급 기업의 계약에 이름 대응을 공유하지 않는다.
    components={}
    for fact in facts:
        if fact['action']!='supply_contract':
            continue
        actor=fact['actor']
        names=party_aliases(fact)
        sets=components.setdefault(actor,[])
        touching=[s for s in sets if s & names]
        merged=set(names)
        for s in touching:
            merged.update(s)
            sets.remove(s)
        if merged:
            sets.append(merged)
    return components


def same_party(left,right,context=None):
    a,b=party_aliases(left),party_aliases(right)
    if a & b:
        return True
    return any(a & names and b & names for names in (context or {}).get(left['actor'],[]))


def region_view(value):
    value=re.sub(r'\s*(?:및|와|과|[·ㆍ,])\s*',',',value)
    broad=bool(re.search(r'해외|등|외$',value))
    value=re.sub(r'해외|등|외$','',value)
    return {v.strip() for v in value.split(',') if v.strip()},broad


def same_region(a,b):
    x,xb=region_view(a)
    y,yb=region_view(b)
    return x==y or bool(xb and x<=y) or bool(yb and y<=x)


def amount_overlap(left,right):
    a,b=left['fields'],right['fields']
    if 'amount_krw' not in a or 'amount_krw' not in b:
        return False
    x,y=Decimal(a['amount_krw']),Decimal(b['amount_krw'])
    rx,ry=Decimal(left.get('amount_resolution_krw','1')),Decimal(right.get('amount_resolution_krw','1'))
    return max(x-rx/2,y-ry/2)<min(x+rx/2,y+ry/2)


def same_product(left,right):
    a,b=left['target'],right['target']
    if a==b:
        return True
    # 포괄적 표현은 계약 식별 항목으로 재확인한다. 상세 표현끼리는 공통 부품명이 필요하다.
    if a.endswith(b) or b.endswith(a):
        return len(min((a,b),key=len))>=2
    x=set(left.get('comparison_evidence',{}).get('product_terms',[]))
    y=set(right.get('comparison_evidence',{}).get('product_terms',[]))
    return len(x & y)>=2


def compatible(left, right, context=None):
    """핵심 역할과 공통 수치가 맞아야 합친다. 금액의 반올림은 다른 수치로 재확인한다."""
    supply=left['action']==right['action']=='supply_contract'
    if supply:
        if any(left[k]!=right[k] for k in ('actor','action','stage')):
            return False
    elif core(left) != core(right):
        return False
    a, b = left['fields'], right['fields']
    shared = a.keys() & b.keys()
    conflicts = {k for k in shared if a[k] != b[k]}
    if supply:
        party_matches=same_party(left,right,context)
        if 'counterparty' in conflicts and party_matches:
            conflicts.remove('counterparty')
        if 'delivery_region' in conflicts and same_region(a['delivery_region'],b['delivery_region']):
            conflicts.remove('delivery_region')
        if left['target']!=right['target']:
            anchor=('contract_end' in shared and a['contract_end']==b['contract_end']) or (
                left.get('announcement_date')==right.get('announcement_date')
                and 'delivery_region' in shared and same_region(a['delivery_region'],b['delivery_region']))
            if not (same_product(left,right) and party_matches and amount_overlap(left,right)
                    and anchor):
                return False
    if left['action']=='supply_contract' and conflicts=={'amount_krw'}:
        overlap=amount_overlap(left,right)
        corroborated=same_party(left,right,context) or ('contract_end' in shared and a['contract_end']==b['contract_end']) or (
            a.get('delivery_region') and b.get('delivery_region') and same_region(a['delivery_region'],b['delivery_region'])
            and left.get('announcement_date')==right.get('announcement_date'))
        if overlap and corroborated:
            conflicts.clear()
    if conflicts == {'amount_krw'} and any(k in shared and a[k] == b[k] for k in ('shares', 'stake_pct')):
        low, high = sorted([Decimal(a['amount_krw']), Decimal(b['amount_krw'])])
        # 억원 단위로만 적힌 기사와 세부 금액 기사가 공존하는 경우만 허용한다.
        if low % 100000000 == 0 and high-low < 50000000:
            conflicts.clear()
    if conflicts:
        return False
    if left['action'] in ('office_open', 'mou'):
        return True
    return bool(shared)


def cluster(rows, cutoff=0.42):
    """추출 가능한 사실은 동일 역할·수치로 묶고 나머지만 기존 문장 비교로 남긴다."""
    import news_review as review
    groups, unknown = [], []
    parsed={a['url']:extract(a) for a in rows}
    context=alias_context([f for p in parsed.values() for f in fact_list(p)])
    for article in sorted(rows, key=lambda a:(a['published_at'], a['url'])):
        extracted = parsed[article['url']]
        candidates=fact_list(extracted)
        if not candidates:
            unknown.append(article)
            continue
        for fact_index,fact in enumerate(candidates):
            matches = [g for g in groups if datetime.fromisoformat(article['published_at'])-
                       datetime.fromisoformat(g['first_published_at']) <= timedelta(hours=review.GROUP_HOURS)
                       and all(compatible(fact, old,context) for old in g['_facts'])]
            # 정보가 부족한 기사가 서로 다른 두 거래에 동시에 맞으면 자동 배정하지 않는다.
            if len(matches) > 1:
                unknown.append(article)
                continue
            if matches:
                group = matches[0]
            else:
                group = {'group_id': hashlib.sha256(('facts:'+article['url']+':'+str(fact_index)).encode()).hexdigest()[:16],
                         'representative_url': article['url'], 'first_published_at': article['published_at'],
                         'title': article['title'], 'members': [], 'grouping_method': VERSION,
                         'primary_fact': fact, '_facts': []}
                groups.append(group)
            group['_facts'].append(fact)
            group['members'].append({'url': article['url'], 'publisher': article.get('publisher'),
                                     'published_at': article['published_at'], 'fact': fact, 'fact_index':fact_index})
    for g in groups:
        g['primary_fact_source_url']=g['representative_url']
        g['primary_fact_source_index']=g['members'][0]['fact_index']
        if g['primary_fact']['action']=='supply_contract':
            best=min(zip(g['_facts'],g['members']),key=lambda pair:(Decimal(pair[0]['amount_resolution_krw']),-len(pair[0]['fields']),pair[1]['published_at'],pair[1]['url']))
            g['primary_fact'],g['primary_fact_source_url']=best[0],best[1]['url']
            g['primary_fact_source_index']=best[1]['fact_index']
        g.pop('_facts')
        g['article_count'] = len({m['url'] for m in g['members']})
        g['publishers'] = sorted({m['publisher'] for m in g['members'] if m['publisher']})
        g['publisher_count'] = len(g['publishers'])
        g['status'] = 'same_fact_candidate' if g['article_count'] > 1 else 'single_article'
    for group in review.cluster(unknown, cutoff):
        group.update(grouping_method='lexical_fallback', fact_hold_reason='fact_extraction_needs_review')
        groups.append(group)
    return sorted(groups, key=lambda g:(g['first_published_at'],g['representative_url']))


def novelty(group, by_url, baseline):
    """공통 사건 골격+비교 가능한 세부 필드의 신규 비율. 추출 불가를 신규로 세지 않는다."""
    import np_scores as np
    current=[]
    for member in group['members']:
        parsed=extract(by_url[member['url']])
        candidates=fact_list(parsed)
        index=member.get('fact_index',0)
        current.append({**parsed,'primary':candidates[index] if index<len(candidates) else None})
    if any(c['primary'] is None for c in current) or group.get('grouping_method') != VERSION:
        return np.metric(reason='fact_extraction_needs_review')
    prior = [(a, extract(a)) for a in baseline]
    known = [(a,{**p,'primary':fact}) for a,p in prior for fact in fact_list(p)]
    if not known:
        return np.metric(reason='no_supported_fact_baseline')
    fact = group['primary_fact']
    context=alias_context([c['primary'] for c in current]+[p['primary'] for _,p in known])
    same_core = [(a,p) for a,p in known if core(p['primary']) == core(fact)
                 or (fact['action']=='supply_contract' and compatible(fact,p['primary'],context))]
    contexts = {context for _,p in prior for context in p['context']}
    group_context = {context for c in current for context in c['context']}
    details = {'formula': 'novel_comparable_fact_components / comparable_fact_components',
               'scope': 'primary_fact_only',
               'primary_fact': fact, 'baseline_article_count': len(baseline),
               'supported_baseline_articles': len({a['url'] for a,_ in known}),
               'supported_baseline_facts':len(known), 'known_context': sorted(group_context & contexts),
               'unverified_details': []}
    if not same_core:
        return np.metric(1.0, core_seen=False, new_components=['actor_action_target_stage'],
                         compared_components=['actor_action_target_stage'], interpretation='new_action_candidate', **details)
    # 대표 기사에 없는 필드는 같은 사실로 묶인 다른 기사에서 보충한다.
    fields, field_sources = {}, {}
    amount_precision=None
    for member, c in zip(group['members'], current):
        for key,value in c['primary']['fields'].items():
            if key=='amount_krw' and fact['action']=='supply_contract':
                precision=Decimal(c['primary']['amount_resolution_krw'])
                if amount_precision is not None and precision>=amount_precision:
                    continue
                amount_precision=precision
            # 억원 반올림 표현이 더 정밀한 금액을 덮어쓰지 않게 한다.
            if key == 'amount_krw' and key in fields and Decimal(fields[key]) % 100000000 != 0 and Decimal(value) % 100000000 == 0:
                continue
            fields[key] = value
            field_sources[key] = member['url']
    details.update(compared_fact_fields=fields, field_sources=field_sources)
    comparisons = []
    for old_article, old in same_core:
        old_fact = old['primary']
        shared = fields.keys() & old_fact['fields'].keys()
        if not shared and fact['action'] not in ('office_open','mou'):
            continue
        changed = sorted(k for k in shared if fields[k] != old_fact['fields'][k])
        if compatible({**fact,'fields':fields},old_fact,context):
            changed = []
        compared = ['actor_action_target_stage']+sorted(shared)
        comparisons.append((len(changed)/len(compared), old_article['url'], changed, compared,
                            sorted(fields.keys()-old_fact['fields'].keys())))
    if not comparisons:
        return np.metric(reason='no_comparable_fact_details', **details)
    value, url, changed, compared, unverified = min(comparisons, key=lambda x:(x[0],x[1]))
    details['unverified_details'] = unverified
    return np.metric(round(value,6), core_seen=True, nearest_prior_url=url,
                     new_components=changed, compared_components=compared,
                     interpretation='new_details_candidate' if changed else 'previous_fact_candidate', **details)
