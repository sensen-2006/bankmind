"""Comprehensive tests for BankMind multi-format financial document parsers & XBRL reconciliation."""
import pytest

from bankmind.parsing import read_table, suggest
from bankmind.xbrl_reconcile import extract_xbrl_cash_facts, reconcile_xbrl_balance


def test_html_and_disguised_xls():
    html_bytes = """<!DOCTYPE html>
    <html><body>
      <table><tr><th>网银导出标题</th></tr><tr><td>模拟工商银行</td></tr></table>
      <table>
        <tr><th>交易日期</th><th>交易金额</th><th>对方名称</th><th>交易摘要</th><th>流水号</th></tr>
        <tr><td>2026-10-05</td><td>12500.50</td><td>云杉材料有限公司</td><td>客户回款</td><td>HTML001</td></tr>
        <tr><td>2026-10-06</td><td>-3200.00</td><td>瑞禾物流有限公司</td><td>运费结算</td><td>HTML002</td></tr>
      </table>
    </body></html>""".encode('utf-8')

    # Standard .html
    res = read_table(html_bytes, '.html')
    assert len(res['rows']) == 2
    assert res['rows'][0]['raw']['对方名称'] == '云杉材料有限公司'
    assert res['suggested_mapping']['date'] == '交易日期'
    assert res['suggested_mapping']['amount'] == '交易金额'

    # HTML-disguised .xls (common in Chinese online banking)
    res_xls = read_table(html_bytes, '.xls')
    assert len(res_xls['rows']) == 2
    assert res_xls['encoding'].startswith('html-')


def test_markdown_pipe_table():
    md_bytes = """# 2026年10月银行对账单

| 交易日期 | 交易金额 | 币种 | 对方名称 | 交易摘要 |
| --- | ---: | --- | --- | --- |
| 2026-10-01 | 88000.00 | CNY | 华星贸易有限公司 | 货款回笼 |
| 2026-10-02 | -1500.25 | CNY | 模拟银行 | 手续费 |
""".encode('utf-8')
    res = read_table(md_bytes, '.md')
    assert len(res['rows']) == 2
    assert res['rows'][1]['raw']['交易金额'] == '-1500.25'
    assert res['suggested_mapping']['counterparty'] == '对方名称'


def test_ascii_pipe_and_aligned_text():
    pipe_txt = (
        "| Date | Amount | Counterparty | Narrative |\n"
        "| 2026-10-10 | 4500.00 | Acme Corp | Invoice 101 |\n"
        "| 2026-10-11 | -210.50 | Cloud Host | Monthly fee |\n"
    ).encode('utf-8')
    res = read_table(pipe_txt, '.txt')
    assert len(res['rows']) == 2
    assert res['suggested_mapping']['date'] == 'Date'
    assert res['suggested_mapping']['description'] == 'Narrative'


def test_swift_mt940_and_strict_controls():
    mt940_bytes = (
        ":20:STMT202610\n"
        ":25:DE89370400440532013000\n"
        ":28C:00001/001\n"
        ":60F:C261001EUR10000,00\n"
        ":61:2610051005C2500,50NTRFNONREF//BANKREF01\n"
        ":86:NAME/Siemens AG/INV-2026-10\n"
        ":61:2610061006D300,25NTRFNONREF//BANKREF02\n"
        ":86:NAME/DHL Express/Freight\n"
        ":62F:C261031EUR12200,25\n"
    ).encode('utf-8')
    res = read_table(mt940_bytes, '.mt940')
    assert len(res['rows']) == 2
    assert res['rows'][0]['raw']['交易金额'] in ('2500.50', '2500.5')
    assert res['rows'][1]['raw']['交易金额'] == '-300.25'
    assert res['rows'][0]['raw']['币种'] == 'EUR'
    # Original printed balances are retained; transaction_count is NOT fabricated!
    controls = res['printed_statement_controls']
    assert controls['opening_balance'] in ('10000.00', '10000')
    assert controls['closing_balance'] in ('12200.25',)
    assert 'transaction_count' not in controls
    assert res['derived_row_count'] == 2


def test_iso20022_camt053_and_xxe_guard():
    xml_bytes = """<?xml version="1.0" encoding="UTF-8"?>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
      <BkToCstmrStmt>
        <Stmt>
          <Acct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></Acct>
          <Bal>
            <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
            <Amt Ccy="GBP">50000.00</Amt>
            <CdtDbtInd>CRDT</CdtDbtInd>
          </Bal>
          <Bal>
            <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
            <Amt Ccy="GBP">51200.00</Amt>
            <CdtDbtInd>CRDT</CdtDbtInd>
          </Bal>
          <TxsSummry><TtlNtries><NbOfNtries>2</NbOfNtries></TtlNtries></TxsSummry>
          <Ntry>
            <Amt Ccy="GBP">1500.00</Amt>
            <CdtDbtInd>CRDT</CdtDbtInd>
            <BookgDt><Dt>2026-10-12</Dt></BookgDt>
            <AcctSvcrRef>CAMT-001</AcctSvcrRef>
            <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>London Supply Ltd</Nm></Dbtr></RltdPties><RmtInf><Ustrd>PO-998</Ustrd></RmtInf></TxDtls></NtryDtls>
          </Ntry>
          <Ntry>
            <Amt Ccy="GBP">300.00</Amt>
            <CdtDbtInd>DBIT</CdtDbtInd>
            <BookgDt><Dt>2026-10-13</Dt></BookgDt>
            <AcctSvcrRef>CAMT-002</AcctSvcrRef>
            <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>UK Logistics</Nm></Cdtr></RltdPties><RmtInf><Ustrd>Shipping</Ustrd></RmtInf></TxDtls></NtryDtls>
          </Ntry>
        </Stmt>
      </BkToCstmrStmt>
    </Document>""".encode('utf-8')

    res = read_table(xml_bytes, '.xml')
    assert len(res['rows']) == 2
    assert res['rows'][0]['raw']['交易金额'] == '1500.00'
    assert res['rows'][1]['raw']['交易金额'] == '-300.00'
    assert res['rows'][0]['raw']['对方名称'] == 'London Supply Ltd'
    assert res['printed_statement_controls']['opening_balance'] == '50000.00'
    assert res['printed_statement_controls']['closing_balance'] == '51200.00'
    assert res['printed_statement_controls']['transaction_count'] == 2

    # XXE must be blocked
    xxe = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Document/>'
    with pytest.raises(ValueError):
        read_table(xxe, '.xml')


def test_ofx_bank_statement():
    ofx_bytes = (
        "OFXHEADER:100\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
        "<CURDEF>USD<BANKACCTFROM><ACCTID>99887766</ACCTID></BANKACCTFROM>"
        "<BANKTRANLIST>"
        "<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261015<TRNAMT>980.40<FITID>OFX1<NAME>Stripe Payout<MEMO>Settlement</STMTTRN>"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261016<TRNAMT>-45.00<FITID>OFX2<NAME>Bank Fee<MEMO>Wire fee</STMTTRN>"
        "</BANKTRANLIST>"
        "<LEDGERBAL><BALAMT>15435.40</BALAMT></LEDGERBAL>"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    ).encode('utf-8')
    res = read_table(ofx_bytes, '.ofx')
    assert len(res['rows']) == 2
    assert res['rows'][0]['raw']['交易日期'] == '2026-10-15'
    assert res['rows'][0]['raw']['交易金额'] == '980.40'
    assert res['rows'][1]['raw']['交易金额'] == '-45.00'
    assert res['printed_statement_controls']['closing_balance'] == '15435.40'


def test_multilingual_header_mapping():
    # German bank headers
    de = suggest(['Buchungsdatum', 'Betrag', 'Währung', 'Auftraggeber', 'Verwendungszweck', 'Saldo'])
    assert de['date'] == 'Buchungsdatum'
    assert de['amount'] == 'Betrag'
    assert de['currency'] == 'Währung'
    assert de['counterparty'] == 'Auftraggeber'
    assert de['description'] == 'Verwendungszweck'
    assert de['balance'] == 'Saldo'

    # Japanese bank headers
    jp = suggest(['取引日', '取引金額', '取引先', '残高'])
    assert jp['date'] == '取引日'
    assert jp['amount'] == '取引金額'
    assert jp['counterparty'] == '取引先'
    assert jp['balance'] == '残高'


def test_xbrl_and_ixbrl_reconciliation_scope_guard():
    ixbrl = """<!DOCTYPE html>
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:us-gaap="http://fasb.org/us-gaap/2025">
      <body>
        <ix:header><ix:resources>
          <xbrli:context id="c2026"><xbrli:period><xbrli:instant>2026-10-31</xbrli:instant></xbrli:period></xbrli:context>
          <xbrli:unit id="uCNY"><xbrli:measure>iso4217:CNY</xbrli:measure></xbrli:unit>
        </ix:resources></ix:header>
        <p>货币资金：<ix:nonFraction name="us-gaap:CashAndCashEquivalentsAtCarryingValue" contextRef="c2026" unitRef="uCNY" scale="3">1,250.50</ix:nonFraction></p>
      </body>
    </html>""".encode('utf-8')

    extracted = extract_xbrl_cash_facts(ixbrl)
    assert extracted['source_format'] == 'iXBRL'
    assert extracted['facts'][0]['amount'] == '1250500.00'

    # Scope NOT confirmed -> MUST return NOT_COMPARABLE (never false discrepancy)
    unconfirmed = reconcile_xbrl_balance(extracted['facts'], 'CNY', '2026-10-31', '1250500.00', scope_confirmed=False)
    assert unconfirmed['status'] == 'NOT_COMPARABLE'

    # Scope confirmed & equal -> MATCHED
    matched = reconcile_xbrl_balance(extracted['facts'], 'CNY', '2026-10-31', '1250500.00', scope_confirmed=True)
    assert matched['status'] == 'MATCHED'
    assert matched['delta'] == '0.00'

    # Scope confirmed & unequal -> DIFFERENCE
    diff = reconcile_xbrl_balance(extracted['facts'], 'CNY', '2026-10-31', '1250000.00', scope_confirmed=True)
    assert diff['status'] == 'DIFFERENCE'
    assert diff['delta'] == '-500.00'
