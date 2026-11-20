"""Pure deterministic engine. Scores are ranking aids, never probabilities."""
from collections import defaultdict, Counter, deque
from datetime import date
from itertools import combinations
from time import monotonic
from decimal import Decimal
from rapidfuzz.fuzz import ratio
from .parsing import normalize_name, amount_text, money

ENGINE_VERSION = 'bankmind-1.1.0'
DEFAULT_RULES = {'date_days': 3, 'amount_absolute': '0', 'amount_relative': '0'}

def delta_days(a, b):
    return abs((date.fromisoformat(a.date) - date.fromisoformat(b.date)).days)

def same_scope(a, b):
    return (a.tenant_id == b.tenant_id and a.account_id == b.account_id and a.currency == b.currency
            and a.amount * b.amount > 0 and not (a.group_key and b.group_key and a.group_key != b.group_key))

def tolerance(amount, currency, rules):
    limits = []
    if Decimal(str(rules.get('amount_absolute', '0'))) > 0:
        limits.append(money(str(rules['amount_absolute']), currency))
    if Decimal(str(rules.get('amount_relative', '0'))) > 0:
        limits.append(int(abs(amount) * Decimal(str(rules['amount_relative']))))
    return min(limits) if limits else 0

def candidate_affinity(a, b):
    name_a, name_b = normalize_name(a.counterparty), normalize_name(b.counterparty)
    desc_a, desc_b = (a.description or '').strip(), (b.description or '').strip()
    name_score = ratio(name_a, name_b) if name_a and name_b else 0.0
    text_score = ratio(desc_a, desc_b) if min(len(desc_a), len(desc_b)) >= 4 else 0.0
    exact_group = int(bool(a.group_key and a.group_key == b.group_key))
    exact_name = int(bool(name_a and name_a == name_b))
    exact_desc = int(bool(desc_a and desc_a == desc_b))
    return (-exact_group, -exact_name, -exact_desc, delta_days(a, b), -round(max(name_score, text_score), 2), b.id)

def run_matching(transactions, rules=None, budget_seconds=2.0):
    rules = {**DEFAULT_RULES, **(rules or {})}
    bank = sorted([t for t in transactions if t.side == 'BANK' and t.quality == 'VALID'], key=lambda t: t.id)
    book = sorted([t for t in transactions if t.side == 'BOOK' and t.quality == 'VALID'], key=lambda t: t.id)
    by_id = {t.id: t for t in bank+book}
    blocked, matches, cases = set(), [], []
    # Assess the entire source set before filtering quarantined rows. A reversal
    # or repeated reference must not make its apparently clean sibling automatic.
    reference_rows = defaultdict(list)
    for t in transactions:
        if t.reference and t.quality != 'EXCLUDED':
            reference_rows[(t.tenant_id,t.account_id,t.currency,t.reference)].append(t)
    review_ids = set()
    for rows in reference_rows.values():
        duplicate = any(sum(t.side == side for t in rows) > 1 for side in ['BANK','BOOK'])
        quarantined = any(t.quality != 'VALID' for t in rows)
        if duplicate or quarantined:
            review_ids.update(t.id for t in rows)
    # A reversal often has its own reference. Same-side opposite cash movements
    # with equal amount/name/date proximity are a risk flag, never a net-off.
    movement_index = defaultdict(list)
    for t in transactions:
        if t.quality!='EXCLUDED' and t.date and normalize_name(t.counterparty):
            movement_index[(t.tenant_id,t.account_id,t.currency,t.side,t.amount,normalize_name(t.counterparty))].append(t)
    overflow_movements=set()
    for t in transactions:
        if t.quality=='EXCLUDED' or not t.date or not ('冲正' in t.description or any(i.get('code')=='REVERSAL_REVIEW' for i in getattr(t,'issues',[]))):continue
        key=(t.tenant_id,t.account_id,t.currency,t.side,-t.amount,normalize_name(t.counterparty))
        pool=movement_index[key]
        # Overflow is conservatively flagged without an unbounded date cross-product.
        if len(pool)>50:
            overflow_movements.add(key);review_ids.add(t.id);continue
        for original in pool:
            if delta_days(t,original)<=rules['date_days']:
                review_ids.update([t.id,original.id])
    for key in overflow_movements:review_ids.update(t.id for t in movement_index[key])
    diagnostics = {'engine': ENGINE_VERSION, 'combination_limit': 3, 'candidate_limit': 12, 'search_status': 'COMPLETE'}
    def proposal(bs, ls, method, score, status, extra=None):
        ids = [t.id for t in bs+ls]
        assert not blocked.intersection(ids)
        blocked.update(ids)
        matches.append({'bank_ids': [t.id for t in bs], 'book_ids': [t.id for t in ls],
                        'method': method, 'score': score, 'status': status,
                        'evidence': {'amount_delta': amount_text(sum(t.amount for t in bs)-sum(t.amount for t in ls), bs[0].currency),
                                     'date_delta_days': max(delta_days(b,l) for b in bs for l in ls),
                                     'predicates': ['同租户', '同银行账户', '同币种', '企业现金方向一致', '未重复占用'],
                                     # Preserve how the programme first classified the group.
                                     # A later human approval must never turn this into automatic coverage.
                                     'program_determined': status == 'DETERMINISTIC',
                                     'source_ids': ids, **(extra or {})}})
    def stage(edges, method, score, auto=False):
        counts = Counter(i for a,b in edges for i in [a.id,b.id])
        ambiguous = {i for i,c in counts.items() if c > 1}
        # Expand all overlapping edges, so no first-row or ordering-based choices.
        adjacency=defaultdict(list)
        for a,b in edges:
            adjacency[a.id].append(b.id);adjacency[b.id].append(a.id)
        queue=deque(sorted(ambiguous))
        while queue:
            current=queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor not in ambiguous:
                    ambiguous.add(neighbor);queue.append(neighbor)
        ambiguous_options=defaultdict(list)
        for a,b in edges:
            if a.id in ambiguous:ambiguous_options[a.id].append(b)
        for a in bank:
            options = sorted(ambiguous_options[a.id], key=lambda b: candidate_affinity(a, b))
            if options:
                cases.append({'kind': 'AMBIGUOUS', 'title': '多个可行候选，需补充交易证据',
                              'transaction_ids': [a.id], 'candidates': [b.id for b in options]})
        blocked.update(ambiguous)
        for a,b in edges:
            if a.id not in blocked and b.id not in blocked:
                risk = bool(review_ids.intersection([a.id,b.id]))
                proposal([a],[b],method,score,'DETERMINISTIC' if auto and not risk else 'PENDING',
                         {'rule': method, 'reference_exact': bool(a.reference and a.reference == b.reference),
                          'auto_blockers': ['重复编号、隔离关联或疑似冲正原交易，必须逐组独立复核'] if risk else [],
                          'text_similarity': round(ratio(normalize_name(a.counterparty), normalize_name(b.counterparty)), 1)})
    # Index candidates: never unrestricted cross products.
    refs, amounts, groups, exact, bank_refs = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for t in bank:
        if t.reference:bank_refs[t.reference].append(t)
    for t in book:
        if t.reference:
            refs[t.reference].append(t)
        amounts[(t.account_id,t.currency,t.amount)].append(t)
        exact[(t.account_id,t.currency,t.amount,t.date,normalize_name(t.counterparty))].append(t)
        if t.group_key:
            groups[t.group_key].append(t)
    for b in bank:
        if b.reference and len(refs[b.reference])>50:
            blocked.add(b.id)
            cases.append({'kind':'CANDIDATE_LIMIT','title':'编号候选超过50条，转人工分桶','transaction_ids':[b.id],'candidates':[]})
    edges = [(b,l) for b in bank if b.reference and b.id not in blocked for l in refs[b.reference]
             if same_scope(b,l) and b.amount == l.amount and delta_days(b,l) <= rules['date_days']]
    stage(edges, 'L1_REFERENCE', 100, True)
    edges = []
    for b in bank:
        if b.id in blocked:
            continue
        pool=exact[(b.account_id,b.currency,b.amount,b.date,normalize_name(b.counterparty))]
        if len(pool)>50:
            blocked.add(b.id)
            cases.append({'kind':'CANDIDATE_LIMIT','title':'精确同层候选超过50条，转人工分桶','transaction_ids':[b.id],'candidates':[]})
            continue
        valid_pool = [l for l in pool if l.id not in blocked and same_scope(b, l)
                      and not (b.reference and l.reference and b.reference != l.reference)
                      and b.date == l.date and normalize_name(b.counterparty)
                      and normalize_name(b.counterparty) == normalize_name(l.counterparty)]
        if len(valid_pool) > 1 and (b.group_key or (b.description or '').strip()):
            refined = [l for l in valid_pool
                       if (not b.group_key or not l.group_key or b.group_key == l.group_key)
                       and (not (b.description or '').strip() or not (l.description or '').strip()
                            or (b.description or '').strip() == (l.description or '').strip())]
            if refined:
                valid_pool = refined
        for l in valid_pool:
            edges.append((b,l))
    stage(edges,'L2_EXACT',98,True)
    edges = []
    for b in bank:
        if b.id in blocked:
            continue
        candidates = amounts[(b.account_id,b.currency,b.amount)]
        if len(candidates) > 50:
            blocked.add(b.id)
            cases.append({'kind':'CANDIDATE_LIMIT', 'title':'同额候选超过50条，转人工分桶', 'transaction_ids':[b.id], 'candidates':[]})
            continue
        for l in candidates:
            if l.id in blocked or not same_scope(b,l) or delta_days(b,l) > rules['date_days']:
                continue
            if b.reference and l.reference and b.reference != l.reference:
                continue
            name_score = ratio(normalize_name(b.counterparty),normalize_name(l.counterparty)) if b.counterparty and l.counterparty else 0
            text_score = ratio(b.description,l.description) if min(len(b.description),len(l.description)) >= 6 else 0
            if max(name_score,text_score) >= 70:
                edges.append((b,l))
    stage(edges,'L3_FUZZY',85)
    # Tolerance only 1:1, only shared strong reference, always suggestion with residual.
    edges = [(b,l) for b in bank if b.id not in blocked and b.reference for l in refs[b.reference]
             if l.id not in blocked and same_scope(b,l) and delta_days(b,l) <= rules['date_days']
             and 0 < abs(b.amount-l.amount) <= tolerance(b.amount,b.currency,rules)]
    stage(edges,'L3_TOLERANCE',80)
    # Bounded 1:N and N:1 search: explicit business batch key first, then bounded shared reference / counterparty-date fallback.
    deadline = monotonic()+budget_seconds
    combos, overflow = [], set()
    def apply_combos(found_combos, rule_desc, key_getter):
        counts = Counter(t.id for bs,ls in found_combos for t in bs+ls)
        for bs,ls in found_combos:
            ids = [t.id for t in bs+ls]
            if any(counts[i] > 1 for i in ids):
                for b in bs:
                    if b.id not in blocked:
                        cases.append({'kind':'AMBIGUOUS','title':'组合合计存在多解，禁止自动选择',
                                      'transaction_ids':[b.id], 'candidates':[t.id for t in ls]})
                blocked.update(ids)
            elif not blocked.intersection(ids):
                risk=bool(review_ids.intersection(ids))
                extra={'group_key':key_getter(bs[0]),'rule':rule_desc}
                if risk:extra['auto_blockers']=['重复编号、隔离关联或疑似冲正，组合必须逐组复核']
                proposal(bs,ls,'L4_ONE_TO_MANY' if len(bs)==1 else 'L4_MANY_TO_ONE',90,'PENDING',extra)
    try:
        for left, right in [(bank,book),(book,bank)]:
            group_index = defaultdict(list)
            for t in right:
                if t.id not in blocked and t.group_key:
                    group_index[t.group_key].append(t)
            for anchor in left:
                if monotonic() > deadline:
                    raise TimeoutError
                if anchor.id in blocked or not anchor.group_key:
                    continue
                pool = [t for t in group_index[anchor.group_key] if same_scope(anchor,t) and delta_days(anchor,t) <= rules['date_days']]
                if len(pool) > 12:
                    overflow.add(anchor.id)
                    continue
                for n in [2,3]:
                    for combo in combinations(pool,n):
                        if monotonic() > deadline:
                            raise TimeoutError
                        if sum(t.amount for t in combo) == anchor.amount:
                            bs,ls = ([anchor],list(combo)) if anchor.side == 'BANK' else (list(combo),[anchor])
                            combos.append((bs,ls))
    except TimeoutError:
        diagnostics['search_status'] = 'TIMEOUT_INCOMPLETE'
        overflow.update(t.id for t in bank+book if t.id not in blocked and t.group_key)
        combos = []
    apply_combos(combos, '同业务批次、最多3笔、精确合计，无组合金额容差', lambda t: t.group_key)
    if diagnostics['search_status'] == 'COMPLETE':
        ref_combos = []
        try:
            for left, right in [(bank,book),(book,bank)]:
                ref_index = defaultdict(list)
                for t in right:
                    if t.id not in blocked and not t.group_key and t.reference:
                        ref_index[(t.account_id, t.currency, t.reference)].append(t)
                for anchor in left:
                    if monotonic() > deadline:
                        raise TimeoutError
                    if anchor.id in blocked or anchor.group_key or not anchor.reference:
                        continue
                    pool = [t for t in ref_index[(anchor.account_id, anchor.currency, anchor.reference)]
                            if same_scope(anchor,t) and delta_days(anchor,t) <= rules['date_days']]
                    if len(pool) > 12:
                        overflow.add(anchor.id)
                        continue
                    for n in [2,3]:
                        for combo in combinations(pool,n):
                            if monotonic() > deadline:
                                raise TimeoutError
                            if sum(t.amount for t in combo) == anchor.amount:
                                bs,ls = ([anchor],list(combo)) if anchor.side == 'BANK' else (list(combo),[anchor])
                                ref_combos.append((bs,ls))
        except TimeoutError:
            diagnostics['search_status'] = 'TIMEOUT_INCOMPLETE'
            ref_combos = []
        apply_combos(ref_combos, '同参考号、最多3笔、精确合计，无组合金额容差', lambda t: t.reference)
    if diagnostics['search_status'] == 'COMPLETE':
        cp_combos = []
        try:
            for left, right in [(bank,book),(book,bank)]:
                cp_index = defaultdict(list)
                for t in right:
                    if t.id not in blocked and not t.group_key and normalize_name(t.counterparty):
                        cp_index[(t.account_id, t.currency, normalize_name(t.counterparty))].append(t)
                for anchor in left:
                    if monotonic() > deadline:
                        raise TimeoutError
                    if anchor.id in blocked or anchor.group_key or not normalize_name(anchor.counterparty):
                        continue
                    pool = [t for t in cp_index[(anchor.account_id, anchor.currency, normalize_name(anchor.counterparty))]
                            if same_scope(anchor,t) and delta_days(anchor,t) <= rules['date_days']
                            and not (anchor.reference and t.reference and anchor.reference != t.reference)]
                    if len(pool) > 12:
                        continue
                    for n in [2,3]:
                        for combo in combinations(pool,n):
                            if monotonic() > deadline:
                                raise TimeoutError
                            if sum(t.amount for t in combo) == anchor.amount:
                                bs,ls = ([anchor],list(combo)) if anchor.side == 'BANK' else (list(combo),[anchor])
                                cp_combos.append((bs,ls))
        except TimeoutError:
            diagnostics['search_status'] = 'TIMEOUT_INCOMPLETE'
            cp_combos = []
        apply_combos(cp_combos, '缺编号同对手同窗口、最多3笔、精确合计，待人工复核确认', lambda t: normalize_name(t.counterparty))
    for i in overflow:
        if i not in blocked:
            cases.append({'kind':diagnostics['search_status'] if diagnostics['search_status'] != 'COMPLETE' else 'CANDIDATE_LIMIT',
                          'title':'组合搜索超时或候选超限，结果不完整，需人工调查', 'transaction_ids':[i],'candidates':[]})
            blocked.add(i)
    # Rule diagnostics; a one-sided record is a fact, never an automatic timing classification.
    for t in bank+book:
        if t.id in blocked:
            continue
        others = book if t.side == 'BANK' else bank
        related = (refs[t.reference] if t.side=='BANK' else bank_refs[t.reference])[:12] if t.reference else []
        kind = 'BANK_ONLY' if t.side == 'BANK' else 'BOOK_ONLY'
        if related:
            if any(t.currency != o.currency for o in related): kind = 'CURRENCY_MISMATCH'
            elif any(t.amount * o.amount < 0 for o in related): kind = 'DIRECTION_MISMATCH'
            elif any(t.amount != o.amount for o in related): kind = 'AMOUNT_DIFFERENCE'
            elif any(delta_days(t,o) > rules['date_days'] for o in related): kind = 'DATE_OUTSIDE'
            else: kind = 'NAME_DIFFERENCE'
        elif '手续费' in t.description:
            kind = 'FEE_REVIEW'
        cases.append({'kind':kind,'title':{'BANK_ONLY':'银行有记录，账面未找到匹配','BOOK_ONLY':'账面有记录，银行未找到匹配',
                      'DIRECTION_MISMATCH':'交易方向不一致','AMOUNT_DIFFERENCE':'参考号相同但金额不一致',
                      'DATE_OUTSIDE':'日期超出规则窗口','FEE_REVIEW':'疑似银行手续费，需回单验证'}.get(kind, '交易差异待调查'),
                      'transaction_ids':[t.id], 'candidates':[o.id for o in related]})
    # A mirrored pair (bank side and book side) is one business question, not two.
    # Keep the primary side for resolution/control totals and attach the opposite
    # record as a related candidate, so users and the AI investigate it once.
    merge_kinds={'AMOUNT_DIFFERENCE','DIRECTION_MISMATCH'}
    index={(c['kind'],c['transaction_ids'][0],c['candidates'][0]):c for c in cases
           if c['kind'] in merge_kinds and len(c['transaction_ids'])==1 and len(c['candidates'])==1}
    dropped=set()
    merged=[]
    for c in cases:
        if id(c) in dropped:
            continue
        if c['kind'] in merge_kinds and len(c['transaction_ids'])==1 and len(c['candidates'])==1:
            mirror=index.get((c['kind'],c['candidates'][0],c['transaction_ids'][0]))
            if mirror is not None and mirror is not c and id(mirror) not in dropped:
                c['candidates']=list(dict.fromkeys(c['candidates']+mirror['transaction_ids']))
                c['title']=c['title']+'（含对侧关联记录）'
                dropped.add(id(mirror))
        merged.append(c)
    cases=merged
    return {'matches':matches,'cases':cases,'diagnostics':diagnostics}
