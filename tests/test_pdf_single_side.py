"""Text-PDF import provenance and genuine single-sided investigation mode."""
import io
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject


def create_task(client, auth, name):
    headers = auth['accountant']
    accounts = client.get('/api/accounts', headers=headers).json()
    account = next(item for item in accounts if item['number_masked'].endswith('6688'))
    result = client.post('/api/tasks', headers=headers, json={
        'name': name, 'account_id': account['id'], 'currency': 'CNY',
        'start_date': '2026-10-01', 'end_date': '2026-10-31',
    })
    assert result.status_code == 200, result.text
    return result.json()


def import_csv(client, auth, task, side, rows):
    headers = auth['accountant']
    content = '日期,金额,账号,币种,参考号,对方名称,摘要\n' + rows
    upload = client.post('/api/tasks/' + task['id'] + '/imports', headers=headers,
                         data={'side': side}, files={'file': ('test.csv', content.encode(), 'text/csv')})
    assert upload.status_code == 200, upload.text
    batch = upload.json()
    mapping = client.post('/api/imports/' + batch['id'] + '/mapping', headers=headers, json={
        'fields': batch['suggested_mapping'], 'confirmed': True, 'direction': 'signed_in',
        'date_format': 'ISO', 'default_currency': 'CNY', 'default_account': '__TASK_ACCOUNT_CONFIRMED__',
    })
    return batch, mapping


def text_pdf(lines):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = DecodedStreamObject()
    body = b'BT /F1 9 Tf 30 740 Td ' + b' 0 -14 Td '.join(
        b'(' + line.encode('latin-1') + b') Tj' for line in lines
    ) + b' ET'
    stream.set_data(body)
    page[NameObject('/Contents')] = writer._add_object(stream)
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})
    })
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload_pdf(client, auth, task, side, content):
    return client.post('/api/tasks/'+task['id']+'/imports', headers=auth['accountant'],
                       data={'side': side}, files={'file': ('bank-statement.pdf', content, 'application/pdf')})


def test_text_pdf_import_page_provenance_and_mapping(client, auth):
    task = create_task(client, auth, 'PDF 文本层导入')
    content = text_pdf([
        'date  amount  reference  counterparty  description',
        '2026-10-01  100.00  PDF-1  Acme  Goods',
        '2026-10-02  -20.00  PDF-2  Beta  Fee',
    ])
    response = upload_pdf(client, auth, task, 'BANK', content)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch['columns'] == ['date', 'amount', 'reference', 'counterparty', 'description']
    assert batch['quality']['encoding'] == 'pdf-text'
    assert batch['quality']['pdf_pages'] == 1
    assert batch['preview'][0]['page'] == 1
    assert batch['preview'][0]['raw']['PDF页码'] == '1'
    mapping = client.post('/api/imports/'+batch['id']+'/mapping', headers=auth['accountant'], json={
        'fields': batch['suggested_mapping'], 'confirmed': True, 'direction': 'signed_in',
        'date_format': 'ISO', 'default_currency': 'CNY', 'default_account': '__TASK_ACCOUNT_CONFIRMED__',
        'source_verified':True, 'statement_controls':{
            'source_note':'原件第 1 页，人工独立核对','transaction_count':2,'receipts':'100.00','payments':'20.00',
            'opening_balance':'1000.00','closing_balance':'1080.00',
        },
    })
    assert mapping.status_code == 200, mapping.text
    assert mapping.json()['valid_count'] == 2


def test_scanned_pdf_is_rejected_with_explicit_ocr_boundary(client, auth):
    task = create_task(client, auth, '扫描 PDF 拒绝')
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    response = upload_pdf(client, auth, task, 'BANK', buffer.getvalue())
    assert response.status_code == 422
    assert '文本层' in response.json()['detail'] or '扫描' in response.json()['detail']

def test_declared_pdf_transaction_count_must_match(client, auth):
    task = create_task(client,auth,'PDF 行数控制')
    response = upload_pdf(client,auth,task,'BANK',text_pdf([
        'Transaction count: 1,000',
        'date  amount  reference',
        '2026-10-01  1.23  QA-1',
    ]))
    assert response.status_code==422
    assert '笔数' in response.json()['detail']


def test_single_sided_task_runs_as_investigation_not_balanced_reconciliation(client, auth):
    task = create_task(client, auth, '单边调查模式')
    _, mapping = import_csv(
        client, auth, task, 'BANK',
        '2026-10-01,100,0000000000006688,CNY,ONE-1,华星贸易,货款\n2026-10-02,-20,0000000000006688,CNY,ONE-2,银行,付款',
    )
    assert mapping.status_code == 200
    # Declaring a verified zero-activity side requires a checkable reason.
    no_reason = client.post('/api/tasks/'+task['id']+'/run', headers=auth['accountant'], json={
        'single_side_declaration': 'CONFIRMED_ZERO', 'reason': '短',
    })
    assert no_reason.status_code == 422
    run = client.post('/api/tasks/'+task['id']+'/run', headers=auth['accountant'], json={
        'single_side_declaration': 'CONFIRMED_ZERO',
        'reason': '已核对银行对账单完整期间，该账户本期无账面记录',
    })
    assert run.status_code == 200, run.text
    assert run.json()['diagnostics']['mode'] == 'SINGLE_SIDE_INVESTIGATION'
    assert run.json()['diagnostics']['single_side'] == ['BOOK']
    assert run.json()['diagnostics']['single_side_declaration'] == 'CONFIRMED_ZERO'
    assert '完整期间' in run.json()['diagnostics']['single_side_reason']
    cases = client.get('/api/cases', headers=auth['accountant'], params={'task_id': task['id']}).json()['items']
    assert cases and all(c['kind'] == 'BANK_ONLY' for c in cases)
    # A draft may disclose the incomplete scope, but it cannot be signed as balanced.
    report = client.post('/api/tasks/'+task['id']+'/reports', headers=auth['accountant'])
    assert report.status_code == 200, report.text
    signed = client.post('/api/reports/'+report.json()['id']+'/sign', headers=auth['reviewer'], json={
        'action': 'APPROVE', 'version': report.json()['version'],
        'reason': '单边任务不应出具已平结论。', 'idempotency_key': 'single-side-sign-check',
    })
    assert signed.status_code == 422
