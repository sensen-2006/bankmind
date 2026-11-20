"""Fixed seed synthetic data. No real enterprise records or bank account."""
import csv
import json
import random
from pathlib import Path
from datetime import date, timedelta

SEED=20261113
ACCOUNT='0000000000006688'
FIELDS=['交易日期','银行流水号','交易金额','币种','本方账号','对方名称','交易摘要','结算批次']

def generate(destination):
    rng=random.Random(SEED); bank=[];book=[];truth=[]
    def row(index,amount,ref='',name='华星贸易有限公司',desc='货款结算',group='',shift=0,currency='CNY'):
        d=date(2026,10,1)+timedelta(days=index%23+shift)
        return dict(zip(FIELDS,[d.isoformat(),ref,f'{amount/100:.2f}',currency,ACCOUNT,name,desc,group]))
    def add(category,bs,ls,match=True):
        bi=list(range(len(bank)+2,len(bank)+len(bs)+2));li=list(range(len(book)+2,len(book)+len(ls)+2))
        bank.extend(bs);book.extend(ls)
        truth.append({'category':category,'bank_rows':bi,'book_rows':li,'true_relation':match})
    for i in range(700):
        a=rng.randint(1000,20000000)*(1 if i%3 else -1);ref=f'BM202610{i:06}'
        add('normal',[row(i,a,ref)],[row(i,a,ref)])
    for i in range(40):
        a=21000001+i*319
        add('name_variation',[row(i,a,name='上海华星贸易有限公司')],[row(i,a,name='上海华星贸业')])
    for i in range(30):
        a=22000001+i*317;ref=f'DATE{i:04}'
        add('date_inside' if i<20 else 'date_outside',[row(i,a,ref)],[row(i,a,ref,shift=2 if i<20 else 6)])
    for i in range(20):
        a,b=23000000+i*313,100000+i*19;group=f'SPLIT{i:03}'
        add('one_to_many',[row(i,a+b,group=group)],[row(i,a,group=group),row(i,b,group=group)])
        group=f'MERGE{i:03}'
        add('many_to_one',[row(i,a,group=group),row(i,b,group=group)],[row(i,a+b,group=group)])
    for i in range(10):
        r=row(i,24000000+i*109,ref=f'DUP{i:03}')
        add('duplicate',[r.copy(),r.copy()],[r.copy(),r.copy()],False)
    for i in range(20): add('fee',[row(i,-1000-i*10,ref=f'FEE{i}',name='模拟银行',desc='银行手续费')],[],False)
    for i in range(10):
        for category,desc in [('refund','客户退款'),('reversal','原交易冲正')]:
            r=row(i,-25000000-i*101,ref=f'{category}{i}',desc=desc)
            add(category,[r.copy()],[r.copy()])
    for i in range(20): add('amount_difference',[row(i,26000000+i*107,ref=f'AMT{i}')],[row(i,26000100+i*107,ref=f'AMT{i}')],False)
    for i in range(10): add('direction_difference',[row(i,27000000+i*103,ref=f'DIR{i}')],[row(i,-27000000-i*103,ref=f'DIR{i}')],False)
    for i in range(10):
        r=row(i,28000000+i*101,name=f'歧义客户{i}')
        # Identical economics with distinct labels: system must request evidence.
        a,b=r.copy(),r.copy();a['交易摘要']='结算单A待补编号';b['交易摘要']='结算单B待补编号'
        add('ambiguous',[a.copy()],[a.copy()])
        add('ambiguous',[b.copy()],[b.copy()])
    for i in range(10): add('currency_mismatch',[row(i,29000000+i*97,ref=f'FX{i}')],[row(i,29000000+i*97,ref=f'FX{i}',currency='USD')],False)
    while len(bank)<1000: add('bank_only',[row(len(bank),30000000+len(bank)*89,ref=f'BANKONLY{len(bank)}')],[],False)
    while len(book)<1000: add('book_only',[],[row(len(book),40000000+len(book)*83,ref=f'BOOKONLY{len(book)}')],False)
    assert len(bank)==len(book)==1000
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for name,rows in [('bank',bank),('book',book)]:
        with (destination/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    (destination/'ground_truth.json').write_text(json.dumps({'synthetic':True,'seed':SEED,'account':ACCOUNT,'relations':truth},ensure_ascii=False,indent=2),encoding='utf-8')
    return bank,book,truth

if __name__=='__main__':
    generate(Path(__file__).resolve().parents[1]/'data/demo')
    print('Generated synthetic bank=1000 book=1000, seed=20261113')
