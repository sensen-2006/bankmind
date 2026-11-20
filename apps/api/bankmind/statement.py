from collections import Counter, defaultdict

from .parsing import amount_text, clean, money

INVALID_FOR_CONTROL = {'AMOUNT_INVALID','DIRECTION_INVALID','CURRENCY_MISMATCH','ACCOUNT_MISMATCH',
                       'DATE_INVALID','OUTSIDE_PERIOD','FORMULA_REJECTED'}


class StatementMismatch(ValueError):
    def __init__(self, mismatches):
        self.mismatches = mismatches


def totals(rows):
    return {'transaction_count':len(rows),'receipts':sum(max(row['amount'],0) for row in rows),
            'payments':sum(max(-row['amount'],0) for row in rows)}


def verify_statement(rows, source_rows, controls, currency, page_count=0, balance_field='', printed=None,
                     *, require_pages=False, require_references=False):
    mismatches=[]

    def check(code, expected, actual, field=None, page=None, row=None):
        if expected!=actual:
            detail={'code':code,'expected':expected,'actual':actual}
            if field is not None:
                detail['field']=field
            if page is not None:
                detail['page']=page
            if row is not None:
                detail['row_no']=row
            mismatches.append(detail)

    def check_money(code, expected, actual, field, **location):
        check(code,amount_text(expected,currency),amount_text(actual,currency),field,**location)

    def compare_totals(actual, expected, code, **location):
        check(code,expected['transaction_count'],actual['transaction_count'],'transaction_count',**location)
        for field in ('receipts','payments'):
            expected_money=money(expected[field],currency)
            if expected_money<0:
                raise ValueError('原件收入和支出总额均须填非负金额')
            check_money(code,expected_money,actual[field],field,**location)

    for field, value in (printed or {}).items():
        if field=='transaction_count':
            check('PRINTED_CONTROL_MISMATCH',value,controls[field],field)
        else:
            check_money('PRINTED_CONTROL_MISMATCH',money(value,currency),money(controls[field],currency),field)
    invalid=[row['row_no'] for row in rows if any(issue['code'] in INVALID_FOR_CONTROL for issue in row['issues'])]
    if invalid:
        mismatches.append({'code':'INVALID_CONTROL_ROWS','count':len(invalid),'sample_rows':invalid[:10]})
    fingerprint_counts=Counter((row['fingerprint'],clean(row['raw'].get(balance_field))) for row in rows)
    repeated=sum(count-1 for count in fingerprint_counts.values() if count>1)
    if repeated:
        mismatches.append({'code':'INDISTINGUISHABLE_ROWS','count':repeated})

    actual=totals(rows)
    compare_totals(actual,controls,'STATEMENT_TOTAL_MISMATCH')
    opening=money(controls['opening_balance'],currency)
    closing=money(controls['closing_balance'],currency)
    declared_closing=opening+money(controls['receipts'],currency)-money(controls['payments'],currency)
    check_money('SOURCE_BALANCE_IDENTITY',closing,declared_closing,'closing_balance')
    computed_closing=opening+actual['receipts']-actual['payments']
    check_money('EXTRACTED_BALANCE_IDENTITY',closing,computed_closing,'closing_balance')

    by_page=defaultdict(list)
    if page_count:
        for row, source in zip(rows,source_rows,strict=True):
            page=source.get('page')
            if type(page) is not int or not 1<=page<=page_count:
                raise ValueError('提取结果缺少有效物理页码')
            by_page[page].append(row)
    pages=controls['pages']
    references_checked=0
    if require_pages and not pages:
        raise ValueError('多模态发布须填写原件各物理页的控制数')
    if pages:
        if not page_count or sorted(page['page'] for page in pages)!=list(range(1,page_count+1)):
            raise ValueError('逐页控制值必须完整覆盖且不得重复，空白页也必须填 0')
        for page in pages:
            page_number=page['page']
            page_rows=by_page[page_number]
            compare_totals(totals(page_rows),page,'PAGE_TOTAL_MISMATCH',page=page_number)
            references=page.get('references')
            if require_references and references is None:
                raise ValueError('多模态发布须按原件顺序填写各页流水号，空白位置填写空字符串')
            if references is not None:
                if len(references)!=page['transaction_count']:
                    raise ValueError('原件流水号列表与该页声明笔数不一致')
                for row, reference in zip(page_rows,references):
                    if clean(row['reference'])!=clean(reference):
                        mismatches.append({'code':'PAGE_REFERENCE_MISMATCH','page':page_number,'row_no':row['row_no']})
                references_checked+=min(len(page_rows),len(references))

    order=controls.get('balance_order')
    if bool(order)!=bool(balance_field):
        raise ValueError('使用逐笔余额时，必须同时映射余额列并声明原件顺序')
    if order:
        running=opening
        ordered=rows if order=='oldest_first' else reversed(rows)
        for row in ordered:
            running+=row['amount']
            try:
                stated=money(row['raw'].get(balance_field),currency)
            except ValueError:
                mismatches.append({'code':'RUNNING_BALANCE_INVALID','row_no':row['row_no']})
            else:
                check_money('RUNNING_BALANCE_MISMATCH',stated,running,'balance',row=row['row_no'])
    if mismatches:
        raise StatementMismatch(mismatches[:20])
    return {'status':'PASS','transaction_count':actual['transaction_count'],
            'receipts':amount_text(actual['receipts'],currency),'payments':amount_text(actual['payments'],currency),
            'opening_balance':amount_text(opening,currency),'closing_balance':amount_text(computed_closing,currency),
            'printed_fields_checked':sorted(printed or {}),'pages_checked':len(pages),
            'references_checked':references_checked,
            'running_balances_checked':len(rows) if order else 0}
