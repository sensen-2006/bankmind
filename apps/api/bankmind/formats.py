"""Deterministic open-source financial document format adapters.

Uses battle-tested open-source libraries (pdfplumber, mt940, defusedxml, lxml,
markdown-it-py) when installed, with strict fail-closed stdlib fallbacks so
every format works deterministically in any Python 3.12/3.13 environment.

Accounting & Security guarantees:
- Zero macro/formula execution, XXE-blocked XML/HTML parsing.
- Exact Decimal arithmetic only (never float).
- Printed statement controls contain ONLY values explicitly present in the source
  document; derived row counts are never disguised as printed source controls.
"""
from __future__ import annotations

import csv
from html.parser import HTMLParser
import io
import re
from decimal import Decimal, InvalidOperation
import unicodedata
import xml.etree.ElementTree as StdET

try:
    from defusedxml import ElementTree as SafeET
except ImportError:
    SafeET = None

try:
    from lxml import html as lxml_html
except ImportError:
    lxml_html = None

try:
    from markdown_it import MarkdownIt
except ImportError:
    MarkdownIt = None

try:
    import mt940 as mt940_lib
except ImportError:
    mt940_lib = None

MAX_ROWS = 20000


def _clean(value) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def _safe_xml_fromstring(content: bytes):
    if SafeET is not None:
        return SafeET.fromstring(content)
    # Fail-closed XXE guard when defusedxml is not installed
    if b'<!ENTITY' in content.upper() or b'<!DOCTYPE' in content.upper():
        raise ValueError('XML 包含禁用声明（DOCTYPE/ENTITY），已拒绝解析')
    return StdET.fromstring(content)


def _decode_text(content: bytes) -> tuple[str, str]:
    for enc in ('utf-8-sig', 'gb18030', 'latin-1'):
        try:
            text = content.decode(enc)
            if '\x00' in text:
                raise ValueError('文件包含空字节，不是有效文本格式')
            return text, enc
        except UnicodeDecodeError:
            continue
    raise ValueError('文本编码不支持，请转为 UTF-8 或 GB18030')


def looks_like_html(content: bytes) -> bool:
    head = content[:2048].lstrip().lower()
    return (
        head.startswith(b'<!doctype html')
        or head.startswith(b'<html')
        or head.startswith(b'<table')
        or head.startswith(b'<meta')
        or (b'<table' in head and b'<tr' in head)
    )


def looks_like_mt940(content: bytes) -> bool:
    head = content[:4096]
    return b':20:' in head and (b':60F:' in head or b':60M:' in head or b':61:' in head)


def _table_score(matrix: list[list[str]]) -> tuple[int, int]:
    if len(matrix) < 2:
        return (0, 0)
    date_re = re.compile(r'20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{8}')
    num_re = re.compile(r'\d+(?:\.\d{1,3})?')
    scored_rows = 0
    max_cols = max((len(r) for r in matrix), default=0)
    if max_cols < 3:
        return (0, 0)
    for row in matrix[1:]:
        joined = ' '.join(row)
        if date_re.search(joined) and num_re.search(joined):
            scored_rows += 1
    return (scored_rows, len(matrix) * max_cols)


class _StdHTMLTableExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == 'table':
            self._table = []
        elif t == 'tr' and self._table is not None:
            self._row = []
        elif t in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ('td', 'th') and self._cell is not None and self._row is not None:
            self._row.append(_clean(''.join(self._cell)))
            self._cell = None
        elif t == 'tr' and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif t == 'table' and self._table is not None:
            if len(self._table) >= 2:
                self.tables.append(self._table)
            self._table = None


def _extract_lxml_matrix(table_el) -> list[list[str]]:
    grid: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}
    for r_idx, tr in enumerate(table_el.xpath('.//tr')):
        row_vals: list[str] = []
        c_idx = 0
        for cell in tr.xpath('./th|./td'):
            while (r_idx, c_idx) in spans:
                row_vals.append(spans.pop((r_idx, c_idx)))
                c_idx += 1
            text = _clean(' '.join(cell.itertext()))
            cs_raw = str(cell.get('colspan') or '1')
            rs_raw = str(cell.get('rowspan') or '1')
            colspan = max(1, min(20, int(cs_raw))) if cs_raw.isdigit() else 1
            rowspan = max(1, min(20, int(rs_raw))) if rs_raw.isdigit() else 1
            for cs in range(colspan):
                row_vals.append(text if cs == 0 else '')
                for rs in range(1, rowspan):
                    spans[(r_idx + rs, c_idx + cs)] = ''
            c_idx += colspan
        while (r_idx, c_idx) in spans:
            row_vals.append(spans.pop((r_idx, c_idx)))
            c_idx += 1
        if any(row_vals):
            grid.append(row_vals)
    return grid


def parse_html_table(content: bytes, sheet: str = '', header_row: int = 1) -> tuple[list[list[str]], list[str], str, str]:
    """Parse HTML or HTML-disguised .xls files into a standard 2D matrix."""
    text, enc = _decode_text(content)
    candidates: list[tuple[str, list[list[str]]]] = []
    if lxml_html is not None:
        parser = lxml_html.HTMLParser(remove_comments=True, encoding='utf-8')
        doc = lxml_html.fromstring(text.encode('utf-8'), parser=parser)
        for idx, tbl in enumerate(doc.xpath('//table'), 1):
            mat = _extract_lxml_matrix(tbl)
            if len(mat) >= 2 and max((len(r) for r in mat), default=0) >= 2:
                candidates.append((f'表格{idx}', mat))
    else:
        ext = _StdHTMLTableExtractor()
        ext.feed(text)
        for idx, mat in enumerate(ext.tables, 1):
            if max((len(r) for r in mat), default=0) >= 2:
                candidates.append((f'表格{idx}', mat))

    if not candidates:
        raise ValueError('HTML 文件中未找到有效的 <table> 表格')
    sheets = [name for name, _ in candidates]
    if sheet:
        selected = next(((n, m) for n, m in candidates if n == sheet), None)
        if not selected:
            raise ValueError(f'工作表 {sheet} 不存在')
        chosen_name, matrix = selected
    else:
        chosen_name, matrix = max(candidates, key=lambda item: _table_score(item[1]))
    return matrix, sheets, chosen_name, f'html-{enc}'


def _parse_md_tables_regex(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    lines = text.splitlines()
    i = 0
    sep_re = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$')
    while i + 1 < len(lines):
        if '|' in lines[i] and sep_re.match(lines[i + 1]):
            def split_md(line: str) -> list[str]:
                s = line.strip()
                if s.startswith('|'):
                    s = s[1:]
                if s.endswith('|'):
                    s = s[:-1]
                return [_clean(c) for c in s.split('|')]

            tbl = [split_md(lines[i])]
            j = i + 2
            while j < len(lines) and '|' in lines[j] and lines[j].strip():
                tbl.append(split_md(lines[j]))
                j += 1
            if len(tbl) >= 2:
                tables.append(tbl)
            i = j
        else:
            i += 1
    return tables


def parse_markdown_table(content: bytes, sheet: str = '', header_row: int = 1) -> tuple[list[list[str]], list[str], str, str]:
    """Parse GFM Markdown pipe tables using markdown-it-py (with stdlib fallback)."""
    text, enc = _decode_text(content)
    tables: list[list[list[str]]] = []
    if MarkdownIt is not None:
        md = MarkdownIt('gfm-like')
        tokens = md.parse(text)
        current_table: list[list[str]] | None = None
        current_row: list[str] | None = None
        in_cell = False
        cell_buf: list[str] = []
        for tok in tokens:
            if tok.type == 'table_open':
                current_table = []
            elif tok.type == 'table_close':
                if current_table:
                    tables.append(current_table)
                current_table = None
            elif tok.type == 'tr_open' and current_table is not None:
                current_row = []
            elif tok.type == 'tr_close' and current_table is not None and current_row is not None:
                current_table.append(current_row)
                current_row = None
            elif tok.type in ('th_open', 'td_open') and current_row is not None:
                in_cell = True
                cell_buf = []
            elif tok.type in ('th_close', 'td_close') and current_row is not None:
                current_row.append(_clean(''.join(cell_buf)))
                in_cell = False
                cell_buf = []
            elif in_cell and tok.type == 'inline':
                cell_buf.append(tok.content or '')
    else:
        tables = _parse_md_tables_regex(text)

    if not tables:
        raise ValueError('Markdown 文件中未找到标准表格（需包含 | 列 | 与 | --- | 分隔行）')
    sheets = [f'表格{i}' for i in range(1, len(tables) + 1)]
    if sheet:
        if sheet not in sheets:
            raise ValueError(f'表格 {sheet} 不存在')
        chosen_name, matrix = sheet, tables[sheets.index(sheet)]
    else:
        best_idx = max(range(len(tables)), key=lambda i: _table_score(tables[i]))
        chosen_name, matrix = sheets[best_idx], tables[best_idx]
    return matrix, sheets, chosen_name, f'markdown-{enc}'


def parse_ascii_text(content: bytes, header_row: int = 1) -> tuple[list[list[str]], str]:
    """Parse delimited (.tsv/.txt/.dat) or whitespace-aligned ASCII bank statements."""
    text, enc = _decode_text(content)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < header_row + 1:
        raise ValueError('文本文件缺少表头或数据行')

    for delim in ('\t', '|', ';', ','):
        counts = [ln.count(delim) for ln in lines[:20]]
        if counts and min(counts[: min(5, len(counts))]) >= 2:
            matrix = []
            for row in csv.reader(io.StringIO('\n'.join(lines)), delimiter=delim):
                cleaned = [_clean(c) for c in row]
                if delim == '|' and len(cleaned) >= 3 and cleaned[0] == '' and cleaned[-1] == '':
                    cleaned = cleaned[1:-1]
                if any(cleaned):
                    matrix.append(cleaned)
            if len(matrix) >= header_row + 1:
                return matrix, f'ascii-delim-{enc}'

    matrix = [[_clean(tok) for tok in re.split(r'\s{2,}', ln.strip()) if tok.strip()] for ln in lines]
    matrix = [r for r in matrix if len(r) >= 2]
    if len(matrix) < header_row + 1:
        raise ValueError('无法识别文本列分隔符（请使用 Tab、竖线 |、逗号或双空格对齐）')
    return matrix, f'ascii-aligned-{enc}'


def _parse_mt940_fallback(text: str, enc: str, suggest_fn) -> dict:
    """Deterministic SWIFT MT940 parser when mt-940 wheel is unavailable."""
    columns = ['交易日期', '交易金额', '币种', '本方账号', '流水号', '对方名称', '交易摘要']
    rows = []
    printed_controls: dict = {}

    acct_m = re.search(r':25:([^\r\n]+)', text)
    acct = _clean(acct_m.group(1)) if acct_m else ''
    currency = ''

    def parse_bal(tag_pat: str) -> tuple[str | None, str]:
        m = re.search(tag_pat, text)
        if not m:
            return None, ''
        dc, _dt, ccy, amt_s = m.group(1), m.group(2), m.group(3), m.group(4).replace(',', '.')
        val = Decimal(amt_s)
        if dc == 'D':
            val = -abs(val)
        return str(val), ccy

    open_val, open_ccy = parse_bal(r':60[FM]:([CD])(\d{6})([A-Z]{3})([\d,]+)')
    close_val, close_ccy = parse_bal(r':62[FM]:([CD])(\d{6})([A-Z]{3})([\d,]+)')
    currency = open_ccy or close_ccy
    if open_val is not None:
        printed_controls['opening_balance'] = open_val
    if close_val is not None:
        printed_controls['closing_balance'] = close_val

    tx_pattern = re.compile(
        r':61:(\d{6})(?:\d{4})?(R?[CD])([A-Z]?)([\d,]+)([A-Z0-9]{4})([^\r\n]*)(?:\r?\n:86:((?:(?!:\d{2}[A-Z]?:).|\r?\n)*))?'
    )
    for idx, m in enumerate(tx_pattern.finditer(text), 1):
        yymmdd, dc, _fc, amt_s, _code, ref_tail, desc_raw = m.groups()
        date_str = f'20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}'
        val = Decimal(amt_s.replace(',', '.'))
        if dc in ('D', 'RC'):
            val = -abs(val)
        ref = _clean(ref_tail.split('//')[0])
        desc = _clean(re.sub(r'\s+', ' ', desc_raw or ''))
        cp_m = re.search(r'(?:NAME|ORDP|BENM)/([^/\n]+)', desc)
        counterparty = _clean(cp_m.group(1)) if cp_m else ''
        rows.append({
            'row_no': idx,
            'raw': {
                '交易日期': date_str,
                '交易金额': str(val),
                '币种': currency,
                '本方账号': acct,
                '流水号': ref,
                '对方名称': counterparty,
                '交易摘要': desc,
            }
        })
        if len(rows) > MAX_ROWS:
            raise ValueError('MT940 超过 20,000 行，请拆分后导入')

    if not rows:
        raise ValueError('MT940 报文中未包含有效交易流水（:61:）')
    return {
        'columns': columns,
        'rows': rows,
        'sheets': [],
        'sheet': '',
        'encoding': f'mt940-{enc}',
        'suggested_mapping': suggest_fn(columns),
        'printed_statement_controls': printed_controls,
        'derived_row_count': len(rows),
    }


def parse_mt940(content: bytes, suggest_fn) -> dict:
    """Parse SWIFT MT940/MT942 ASCII bank statements with exact Decimal precision."""
    text, enc = _decode_text(content)
    if mt940_lib is None:
        return _parse_mt940_fallback(text, enc, suggest_fn)

    parsed = mt940_lib.parse(text)
    if not parsed:
        raise ValueError('MT940 报文为空或格式无效')

    columns = ['交易日期', '交易金额', '币种', '本方账号', '流水号', '对方名称', '交易摘要']
    rows = []
    printed_controls: dict = {}

    raw_data = getattr(parsed, 'data', {}) or {}
    acct = _clean(raw_data.get('account_identification', ''))
    open_bal = raw_data.get('final_opening_balance') or raw_data.get('opening_balance')
    close_bal = raw_data.get('final_closing_balance') or raw_data.get('closing_balance')
    if open_bal and getattr(open_bal, 'amount', None) is not None:
        printed_controls['opening_balance'] = str(open_bal.amount.amount)
    if close_bal and getattr(close_bal, 'amount', None) is not None:
        printed_controls['closing_balance'] = str(close_bal.amount.amount)

    for idx, tx in enumerate(parsed, 1):
        d = tx.data or {}
        amt_obj = d.get('amount')
        if amt_obj is None:
            continue
        amount_val = amt_obj.amount
        if not isinstance(amount_val, Decimal):
            amount_val = Decimal(str(amount_val))
        currency = _clean(getattr(amt_obj, 'currency', '') or '')
        tx_date = d.get('date') or d.get('entry_date')
        date_str = tx_date.isoformat() if hasattr(tx_date, 'isoformat') else _clean(tx_date)
        ref = _clean(d.get('bank_reference') or d.get('customer_reference') or d.get('transaction_reference') or '')
        counterparty = _clean(d.get('applicant_name') or d.get('extra_details') or '')
        desc = _clean(d.get('transaction_details') or d.get('purpose') or '')
        if not counterparty and desc:
            m = re.search(r'(?:NAME|ORDP|BENM)/([^/\n]+)', desc)
            if m:
                counterparty = _clean(m.group(1))
        rows.append({
            'row_no': idx,
            'raw': {
                '交易日期': date_str,
                '交易金额': str(amount_val),
                '币种': currency,
                '本方账号': acct,
                '流水号': ref,
                '对方名称': counterparty,
                '交易摘要': desc,
            }
        })
        if len(rows) > MAX_ROWS:
            raise ValueError('MT940 超过 20,000 行，请拆分后导入')

    if not rows:
        raise ValueError('MT940 报文中未包含有效交易流水（:61:）')

    return {
        'columns': columns,
        'rows': rows,
        'sheets': [],
        'sheet': '',
        'encoding': f'mt940-{enc}',
        'suggested_mapping': suggest_fn(columns),
        'printed_statement_controls': printed_controls,
        'derived_row_count': len(rows),
    }


def _xml_text(el, path: str) -> str:
    found = el.find(path)
    return _clean(found.text) if found is not None and found.text else ''


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def parse_camt053(content: bytes, suggest_fn) -> dict:
    """Parse ISO 20022 camt.053 / camt.052 XML bank statements (XXE-safe)."""
    try:
        root = _strip_ns(_safe_xml_fromstring(content))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('XML 报文解析失败，请确认是合法的 ISO 20022 camt.053/camt.052 文件') from exc

    stmts = root.findall('.//Stmt') or root.findall('.//Rpt')
    if not stmts:
        raise ValueError('XML 中未找到 ISO 20022 <Stmt> 或 <Rpt> 对账单节点')

    columns = ['交易日期', '交易金额', '币种', '本方账号', '流水号', '对方名称', '交易摘要']
    rows = []
    printed_controls: dict = {}

    for stmt in stmts:
        acct = _xml_text(stmt, './Acct/Id/IBAN') or _xml_text(stmt, './Acct/Id/Othr/Id')
        for bal in stmt.findall('./Bal'):
            code = _xml_text(bal, './Tp/CdOrPrtry/Cd')
            amt_el = bal.find('./Amt')
            if amt_el is None or not amt_el.text:
                continue
            try:
                val = Decimal(_clean(amt_el.text))
            except InvalidOperation:
                continue
            ind = _xml_text(bal, './CdtDbtInd').upper()
            if ind == 'DBIT':
                val = -abs(val)
            if code in ('OPBD', 'PRCD', 'OPAV') and 'opening_balance' not in printed_controls:
                printed_controls['opening_balance'] = str(val)
            elif code in ('CLBD', 'CLAV'):
                printed_controls['closing_balance'] = str(val)

        ttl = _xml_text(stmt, './TxsSummry/TtlNtries/NbOfNtries')
        if ttl.isdigit():
            printed_controls['transaction_count'] = int(ttl)

        for ntry in stmt.findall('./Ntry'):
            amt_el = ntry.find('./Amt')
            if amt_el is None or not amt_el.text:
                continue
            try:
                val = Decimal(_clean(amt_el.text))
            except InvalidOperation:
                raise ValueError('ISO 20022 报文含无效金额格式')
            currency = _clean(amt_el.attrib.get('Ccy', ''))
            ind = _xml_text(ntry, './CdtDbtInd').upper()
            if ind == 'DBIT':
                val = -abs(val)
            elif ind == 'CRDT':
                val = abs(val)
            date_str = (
                _xml_text(ntry, './BookgDt/Dt')
                or _xml_text(ntry, './BookgDt/DtTm')[:10]
                or _xml_text(ntry, './ValDt/Dt')
                or _xml_text(ntry, './ValDt/DtTm')[:10]
            )
            ref = (
                _xml_text(ntry, './AcctSvcrRef')
                or _xml_text(ntry, './NtryDtls/TxDtls/Refs/EndToEndId')
                or _xml_text(ntry, './NtryDtls/TxDtls/Refs/TxId')
            )
            counterparty = (
                _xml_text(ntry, './NtryDtls/TxDtls/RltdPties/Dbtr/Nm')
                if ind == 'CRDT'
                else _xml_text(ntry, './NtryDtls/TxDtls/RltdPties/Cdtr/Nm')
            ) or _xml_text(ntry, './NtryDtls/TxDtls/RltdPties/Dbtr/Nm') or _xml_text(ntry, './NtryDtls/TxDtls/RltdPties/Cdtr/Nm')
            desc = _xml_text(ntry, './NtryDtls/TxDtls/RmtInf/Ustrd') or _xml_text(ntry, './AddtlNtryInf')
            rows.append({
                'row_no': len(rows) + 1,
                'raw': {
                    '交易日期': date_str,
                    '交易金额': str(val),
                    '币种': currency,
                    '本方账号': acct,
                    '流水号': ref,
                    '对方名称': counterparty,
                    '交易摘要': desc,
                }
            })
            if len(rows) > MAX_ROWS:
                raise ValueError('ISO 20022 报文超过 20,000 行，请拆分后导入')

    if not rows:
        raise ValueError('ISO 20022 报文中未包含有效 <Ntry> 交易记录')
    if 'transaction_count' in printed_controls and printed_controls['transaction_count'] != len(rows):
        raise ValueError(
            f"ISO 20022 报文声明笔数 ({printed_controls['transaction_count']}) 与解析笔数 ({len(rows)}) 不一致"
        )

    return {
        'columns': columns,
        'rows': rows,
        'sheets': [],
        'sheet': '',
        'encoding': 'iso20022-camt053',
        'suggested_mapping': suggest_fn(columns),
        'printed_statement_controls': printed_controls,
        'derived_row_count': len(rows),
    }


def _ofx_tag(block: str, tag: str) -> str:
    m = re.search(rf'<{tag}>([^<\r\n]+)', block, re.IGNORECASE)
    return _clean(m.group(1)) if m else ''


def parse_ofx(content: bytes, suggest_fn) -> dict:
    """Parse OFX / QFX (both SGML 1.x and XML 2.x) bank statements."""
    text, enc = _decode_text(content)
    currency = _ofx_tag(text, 'CURDEF')
    acct = _ofx_tag(text, 'ACCTID')
    printed_controls: dict = {}
    bal_block = re.search(r'<LEDGERBAL>(.*?)</LEDGERBAL>', text, re.IGNORECASE | re.DOTALL)
    if bal_block:
        balamt = _ofx_tag(bal_block.group(1), 'BALAMT')
        if balamt:
            try:
                printed_controls['closing_balance'] = str(Decimal(balamt))
            except InvalidOperation:
                pass

    tx_blocks = re.findall(r'<STMTTRN>(.*?)</STMTTRN>', text, re.IGNORECASE | re.DOTALL)
    if not tx_blocks:
        parts = re.split(r'<STMTTRN>', text, flags=re.IGNORECASE)
        tx_blocks = parts[1:] if len(parts) > 1 else []
    if not tx_blocks:
        raise ValueError('OFX/QFX 文件中未找到 <STMTTRN> 交易明细')

    columns = ['交易日期', '交易金额', '币种', '本方账号', '流水号', '对方名称', '交易摘要']
    rows = []
    for idx, blk in enumerate(tx_blocks, 1):
        raw_dt = _ofx_tag(blk, 'DTPOSTED')
        m_dt = re.match(r'^(\d{4})(\d{2})(\d{2})', raw_dt)
        date_str = f'{m_dt.group(1)}-{m_dt.group(2)}-{m_dt.group(3)}' if m_dt else raw_dt
        raw_amt = _ofx_tag(blk, 'TRNAMT')
        if not raw_amt:
            continue
        try:
            val = Decimal(raw_amt)
        except InvalidOperation:
            raise ValueError(f'OFX 第 {idx} 笔金额无效：{raw_amt}')
        rows.append({
            'row_no': len(rows) + 1,
            'raw': {
                '交易日期': date_str,
                '交易金额': str(val),
                '币种': currency,
                '本方账号': acct,
                '流水号': _ofx_tag(blk, 'FITID') or _ofx_tag(blk, 'CHECKNUM'),
                '对方名称': _ofx_tag(blk, 'NAME'),
                '交易摘要': _ofx_tag(blk, 'MEMO'),
            }
        })
        if len(rows) > MAX_ROWS:
            raise ValueError('OFX/QFX 超过 20,000 行，请拆分后导入')

    if not rows:
        raise ValueError('OFX/QFX 文件未解析出有效交易行')

    return {
        'columns': columns,
        'rows': rows,
        'sheets': [],
        'sheet': '',
        'encoding': f'ofx-{enc}',
        'suggested_mapping': suggest_fn(columns),
        'printed_statement_controls': printed_controls,
        'derived_row_count': len(rows),
    }


def parse_pdf_with_pdfplumber(content: bytes, suggest_fn, printed_controls_fn) -> dict | None:
    """Enhanced PDF table extraction using pdfplumber vector lines & coordinates."""
    try:
        import pdfplumber
    except ImportError:
        return None

    date_like = re.compile(r'(?<!\d)(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{8})(?!\d)')
    total_re = re.compile(r'^(?:合计|小计|总计|grand total|total|subtotal)(?:\s|[:：]|$)', re.I)

    columns: list[str] = []
    rows: list[dict] = []
    all_lines: list[tuple[int, str]] = []
    page_count = 0

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)
        if page_count > 200:
            raise ValueError('PDF超过200页，请拆分后导入')
        for page_no, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ''
            for ln in text.splitlines():
                if ln.strip():
                    all_lines.append((page_no, _clean(ln)))

            for tbl in (page.extract_tables() or []):
                for raw_row in tbl:
                    if not raw_row:
                        continue
                    cleaned = [_clean(re.sub(r'\s*\n\s*', ' ', str(c or ''))) for c in raw_row]
                    if not any(cleaned):
                        continue
                    if not columns:
                        non_empty = [c for c in cleaned if c]
                        if len(non_empty) >= 3 and len(set(non_empty)) == len(non_empty):
                            mapped = suggest_fn(cleaned)
                            if mapped.get('date') and (mapped.get('amount') or (mapped.get('debit') and mapped.get('credit'))):
                                columns = cleaned
                                continue
                    else:
                        if cleaned == columns:
                            continue
                        first_text = next((c for c in cleaned if c), '')
                        if total_re.match(first_text):
                            continue
                        if len(cleaned) != len(columns):
                            if date_like.search(' '.join(cleaned)):
                                return None
                            continue
                        if not any(re.search(r'\d', c) for c in cleaned):
                            continue
                        raw_dict = {c: cleaned[i] for i, c in enumerate(columns) if c}
                        raw_dict['PDF页码'] = str(page_no)
                        rows.append({'row_no': len(rows) + 1, 'raw': raw_dict, 'page': page_no})
                        if len(rows) > MAX_ROWS:
                            raise ValueError('PDF超过20,000行，请拆分后导入')

    if not columns or not rows:
        return None

    clean_cols = [c for c in columns if c]
    printed = printed_controls_fn(all_lines)
    if 'transaction_count' in printed and printed['transaction_count'] != len(rows):
        raise ValueError('PDF标注的交易笔数与解析行数不一致，未导入；请用原始表格或逐页核对')

    return {
        'columns': clean_cols,
        'rows': rows,
        'sheets': [],
        'sheet': '',
        'encoding': 'pdf-plumber',
        'pdf_pages': page_count,
        'suggested_mapping': suggest_fn(clean_cols),
        'printed_statement_controls': printed,
        'derived_row_count': len(rows),
    }
