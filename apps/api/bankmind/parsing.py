"""Untrusted input parser. No macro/formula evaluation; source rows retained."""
import csv
import io
import re
import zipfile
import unicodedata
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import openpyxl

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20000
PRECISION = {'CNY': 2, 'USD': 2, 'EUR': 2, 'HKD': 2, 'JPY': 0, 'KWD': 3}
ALIASES = {
    'date': [
        '交易日期', '记账日期', '入账日期', '起息日', '日期', '交易时间',
        'date', 'booking_date', 'value_date', 'posting_date', 'transaction_date',
        'buchungsdatum', 'valuta', '取引日', '取引日付', '交易日',
    ],
    'amount': [
        '发生额', '交易金额', '金额', '收支金额', '交易额',
        'amount', 'amount_signed', 'transaction_amount', 'net_amount',
        'betrag', '取引金額', '金額',
    ],
    'debit': [
        '借方发生额', '借方金额', '借方', '收入', '收入金额', '转入金额', '贷记金额',
        'debit', 'debit_amount', 'paid_in', 'credit_in', 'soll', '入金', '借方發生額',
    ],
    'credit': [
        '贷方发生额', '贷方金额', '贷方', '支出', '支出金额', '转出金额', '借记金额',
        'credit', 'credit_amount', 'paid_out', 'haben', '出金', '貸方發生額',
    ],
    'currency': ['币种', '货币', '幣種', 'currency', 'ccy', 'währung', '通貨'],
    'account': [
        '本方账号', '银行账号', '账号', '卡号', '账户号', '本方帳號',
        'account', 'account_number', 'iban', 'konto', '口座番号',
    ],
    'reference': [
        '银行流水号', '交易编号', '流水号', '参考号', '业务参考号', '凭证号', '交易流水号', '核心流水号',
        'reference', 'ref', 'transaction_id', 'bank_reference', 'end_to_end_id', 'referenz', '取引番号',
    ],
    'counterparty': [
        '对方名称', '交易对手', '交易对方', '对方户名', '对方账户名称', '收/付款人名称', '交易對手', '對方戶名',
        'counterparty', 'payee', 'payer', 'beneficiary', 'merchant', 'auftraggeber', 'begünstigter', '取引先',
    ],
    'description': [
        '交易摘要', '摘要', '用途', '附言', '备注', '交易附言', '業務摘要',
        'description', 'narrative', 'memo', 'purpose', 'remittance', 'verwendungszweck', '摘要内容', '明細',
    ],
    'group_key': ['结算批次', '批次号', '业务组键', '汇总号', 'group_key', 'batch_id', 'batch_no'],
    'balance': [
        '账户余额', '余额', '当前余额', '联机余额', '賬戶餘額',
        'balance', 'account_balance', 'running_balance', 'closing_balance', 'saldo', '残高',
    ],
}

def clean(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()

def normalize_name(value):
    return re.sub(r'[\s()（）·,，.。]', '', clean(value)).replace('有限责任公司', '').replace('有限公司', '')

def money(value, currency='CNY'):
    s = clean(value).replace(',', '').replace('¥', '').replace('￥', '').replace('$', '').replace('€', '').replace('−', '-')
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    elif s.endswith('-') and not s.startswith('-'):
        s = '-' + s[:-1]
    s = re.sub(r'(?<=\d)\s*\.\s*(?=\d{1,3}$)', '.', s)
    try:
        d = Decimal(s)
        scale = 10 ** PRECISION[currency]
        if not d.is_finite() or d * scale != (d * scale).to_integral_value() or abs(d) > Decimal('10000000000000'):
            raise ValueError('金额非有限数、超过币种精度或范围')
        return int(d * scale)
    except (InvalidOperation, KeyError):
        raise ValueError('金额类型错误或币种不支持')

def amount_text(value, currency='CNY'):
    p = PRECISION.get(currency, 2)
    return f'{Decimal(value) / Decimal(10 ** p):.{p}f}'

def parse_date(value, fmt):
    value = clean(value)
    patterns = {'ISO': ['%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'],
                'DMY': ['%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y'], 'MDY': ['%m/%d/%Y', '%m-%d-%Y', '%m.%d.%Y']}
    for pattern in patterns.get(fmt, []):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError('日期格式错误或歧义；请确认 ISO/DMY/MDY')

def suggest(columns):
    """Two-tier header suggestion: L1 exact multilingual dictionary + L2 RapidFuzz fuzzy match."""
    # L1: Exact match against multilingual bank alias dictionary
    result = {}
    used = set()
    norm_cols = [(c, clean(c).lower()) for c in columns]
    for field, aliases in ALIASES.items():
        alias_set = {a.lower() for a in aliases}
        hit = next((c for c, nc in norm_cols if nc in alias_set and c not in used), '')
        result[field] = hit
        if hit:
            used.add(hit)

    # L2: Conservative RapidFuzz match for remaining unmapped columns (skip debit/credit to avoid direction confusion)
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return result

    fuzzy_safe_fields = [f for f in ALIASES if f not in ('debit', 'credit') and not result.get(f)]
    for field in fuzzy_safe_fields:
        aliases = [a.lower() for a in ALIASES[field]]
        best_col, best_score = '', 0.0
        for c, nc in norm_cols:
            if not nc or c in used:
                continue
            score = max((fuzz.ratio(nc, a) for a in aliases), default=0.0)
            if score >= 82.0 and score > best_score:
                best_col, best_score = c, score
        if best_col:
            result[field] = best_col
            used.add(best_col)
    return result

def read_table(content, extension, sheet='', header_row=1):
    if not 1 <= header_row <= 20:
        raise ValueError('表头行必须在1–20之间')
    if not content or len(content) > MAX_BYTES:
        raise ValueError('文件为空或超过10 MB')
    sheets, encoding = [], ''
    if extension == '.csv':
        for encoding in ['utf-8-sig', 'gb18030']:
            try:
                decoded = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError('CSV编码不支持，请转为UTF-8或GB18030')
        if '\x00' in decoded:
            raise ValueError('文件不是有效CSV文本')
        try:
            dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        matrix = list(csv.reader(io.StringIO(decoded), dialect))
    elif extension == '.xlsx':
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise ValueError('XLSX损坏或内容类型不符')
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if sum(i.file_size for i in z.infolist()) > 80*1024*1024 or any('vbaProject' in n for n in z.namelist()):
                raise ValueError('工作簿解压大小超限或含宏')
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        sheets = wb.sheetnames
        if sheet and sheet not in sheets:
            raise ValueError('工作表不存在')
        ws = wb[sheet or sheets[0]]
        matrix = []
        if ws.max_row and ws.max_row > MAX_ROWS + 20:
            raise ValueError('工作表超过20,000行')
        for cells in ws.iter_rows():
            line = []
            for c in cells:
                v = c.value
                if c.data_type == 'f':
                    v = '#FORMULA_REJECTED:' + str(v)
                elif isinstance(v, (datetime, date)):
                    v = v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
                line.append('' if v is None else str(v))
            matrix.append(line)
        wb.close()
        sheet = ws.title
    elif extension == '.pdf':
        from .formats import parse_pdf_with_pdfplumber
        plumber_table = parse_pdf_with_pdfplumber(content, suggest, pdf_printed_controls)
        if plumber_table is not None:
            return plumber_table
        from pypdf import PdfReader
        try:
            reader=PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                try:
                    if not reader.decrypt(''):raise ValueError('PDF已加密，无法读取文本层')
                except Exception:raise ValueError('PDF已加密，无法读取文本层')
            if len(reader.pages)>200:raise ValueError('PDF超过200页，请拆分后导入')
            lines=pdf_lines(reader,'plain')
        except ValueError:
            raise
        except Exception:
            raise ValueError('PDF文本层解析失败；请导出为CSV/XLSX，或使用已支持的文本型PDF')
        if len(lines)<2 or not any(re.search(r'\d',line) for _,line in lines):
            raise ValueError('PDF没有可用的表格文本；请上传原始CSV/XLSX，或在已启用时授权视觉提取')
        header_index,columns=pdf_header(lines)
        if header_index is None:
            try:
                positioned=pdf_lines(reader,'layout',keep_indent=True)
            except Exception:
                raise ValueError('PDF无法可靠还原表格位置；请使用原始CSV/XLSX或人工核验') from None
            positioned_table=pdf_cash_layout(positioned,len(reader.pages))
            if positioned_table is not None:
                return positioned_table
            lines=[(page,line.strip()) for page,line in positioned]
            header_index,columns=pdf_header(lines)
        if header_index is None:
            raise ValueError('PDF未找到可可靠切分的表头（至少需要日期/金额等字段和3列）；请上传CSV/XLSX或人工整理后的文本PDF')
        rows=[];skipped=0
        date_like=re.compile(r'(?<!\d)(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{8})(?!\d)')
        for page_no,line in lines[header_index+1:]:
            tokens=[t for t in re.split(r'\t+|\s{2,}',line) if t]
            if tokens==columns or re.match(r'^(?:合计|小计|总计|grand total|total|subtotal)(?:\s|[:：]|$)',line,re.I):continue
            if len(tokens)!=len(columns) and date_like.search(line):
                raise ValueError(f'PDF第{page_no}页有可能是交易的行无法按表头切分；请核对原文件并改用原始表格导出或视觉提取')
            if len(tokens)!=len(columns) or not any(re.search(r'\d',token) for token in tokens):
                skipped+=1;continue
            raw={c:tokens[i] for i,c in enumerate(columns)}
            raw['PDF页码']=str(page_no)
            rows.append({'row_no':len(rows)+1,'raw':raw,'page':page_no})
            if len(rows)>MAX_ROWS:raise ValueError('PDF超过20,000行，请拆分后导入')
        if not rows:
            raise ValueError('PDF表头已识别，但未找到可核对的数据行；请检查是否为扫描件或列对齐')
        if skipped and skipped>max(20,len(rows)//2):
            raise ValueError(f'PDF有{skipped}行无法按表头切分，保守拒绝整份导入；请转为CSV/XLSX后核对')
        printed=pdf_printed_controls(lines[:header_index])
        if 'transaction_count' in printed and printed['transaction_count']!=len(rows):
            raise ValueError('PDF标注的交易笔数与解析行数不一致，未导入；请用原始表格或逐页核对')
        return {'columns':columns,'rows':rows,'sheets':[],'sheet':'','encoding':'pdf-text','pdf_pages':len(reader.pages),
                'suggested_mapping':suggest(columns),'printed_statement_controls':printed}
    elif extension == '.xls':
        from .formats import looks_like_html, parse_html_table
        if looks_like_html(content):
            matrix, sheets, sheet, encoding = parse_html_table(content, sheet, header_row)
        else:
            if not content.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
                raise ValueError('XLS损坏或非二进制Excel格式')
            # BIFF formula records carry cached values which xlrd would otherwise silently read.
            # Reject them conservatively instead of trusting potentially stale formula caches.
            import struct
            from xlrd.compdoc import CompDoc
            compound=CompDoc(content,logfile=io.StringIO())
            stream=compound.get_named_stream('Workbook') or compound.get_named_stream('Book')
            if stream is None:raise ValueError('XLS缺少工作簿数据流')
            offset=0
            while offset+4<=len(stream):
                opcode,size=struct.unpack_from('<HH',stream,offset)
                if opcode in [0x0006,0x0206,0x0406]:raise ValueError('XLS含公式缓存；请提供已核验的静态值文件')
                offset+=4+size
            import xlrd
            book = xlrd.open_workbook(file_contents=content,on_demand=True)
            sheets = book.sheet_names()
            if sheet and sheet not in sheets:
                raise ValueError('工作表不存在')
            sheet = sheet or sheets[0]
            ws=book.sheet_by_name(sheet)
            if ws.nrows>MAX_ROWS+20:raise ValueError('工作表超过20,000行')
            matrix=[]
            for row_index in range(ws.nrows):
                values=[]
                for cell in ws.row(row_index):
                    if cell.ctype==xlrd.XL_CELL_DATE:
                        value=xlrd.xldate_as_datetime(cell.value,book.datemode).date().isoformat()
                    elif cell.ctype==xlrd.XL_CELL_NUMBER:
                        value=str(int(cell.value)) if cell.value==int(cell.value) else str(cell.value)
                    else:value=str(cell.value) if cell.value is not None else ''
                    values.append(value)
                matrix.append(values)
            book.release_resources()
    elif extension in ('.html', '.htm'):
        from .formats import parse_html_table
        matrix, sheets, sheet, encoding = parse_html_table(content, sheet, header_row)
    elif extension in ('.md', '.markdown'):
        from .formats import parse_markdown_table
        matrix, sheets, sheet, encoding = parse_markdown_table(content, sheet, header_row)
    elif extension in ('.sta', '.mt940', '.940'):
        from .formats import parse_mt940
        return parse_mt940(content, suggest)
    elif extension == '.xml':
        from .formats import parse_camt053
        return parse_camt053(content, suggest)
    elif extension in ('.ofx', '.qfx'):
        from .formats import parse_ofx
        return parse_ofx(content, suggest)
    elif extension in ('.txt', '.tsv', '.dat'):
        from .formats import looks_like_html, looks_like_mt940, parse_ascii_text, parse_html_table, parse_mt940
        if looks_like_mt940(content):
            return parse_mt940(content, suggest)
        if looks_like_html(content):
            matrix, sheets, sheet, encoding = parse_html_table(content, sheet, header_row)
        else:
            matrix, encoding = parse_ascii_text(content, header_row)
    else:
        raise ValueError('支持 CSV、XLSX、XLS、PDF、HTML、Markdown、TXT/TSV、MT940、ISO 20022 XML 及 OFX/QFX 格式')
    if len(matrix) < header_row + 1:
        raise ValueError('缺少表头或数据行')
    columns = [clean(v) for v in matrix[header_row-1]]
    while columns and not columns[-1]:
        columns.pop()
    if not columns or len(columns) > 100 or len(set(columns)) != len(columns) or any(not c for c in columns):
        raise ValueError('列名不能为空、重复或超过100列')
    rows = []
    for index, values in enumerate(matrix[header_row:], header_row+1):
        if any(clean(v) for v in values):
            if len(values) > len(columns) and any(clean(v) for v in values[len(columns):]):
                raise ValueError(f'第{index}行列数超出表头')
            rows.append({'row_no': index, 'raw': {c: clean(values[i]) if i < len(values) else '' for i,c in enumerate(columns)}})
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError('有效数据为空或超过20,000行')
    return {'columns': columns, 'rows': rows, 'sheets': sheets, 'sheet': sheet, 'encoding': encoding, 'suggested_mapping': suggest(columns)}

def pdf_lines(reader, mode, keep_indent=False):
    lines=[]
    for page_no,page in enumerate(reader.pages,1):
        text=page.extract_text(extraction_mode=mode) or ''
        if not text.strip():raise ValueError(f'PDF第{page_no}页无可核对文本层；请使用原始表格导出，或在已启用时授权视觉提取')
        if len(text)>2_000_000:raise ValueError('PDF文本层过大，请拆分后导入')
        for raw in text.splitlines():
            line=unicodedata.normalize('NFKC',raw).rstrip()
            if line.strip():
                lines.append((page_no,line if keep_indent else line.strip()))
    return lines

PDF_CELL=re.compile(r'\S(?:.*?\S)?(?=\s{2,}|\s*$)')
PDF_DATE=re.compile(r'(?:[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}|[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4}|[0-9]{8})')
PDF_CASH=re.compile(r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,3})?')
PDF_PAGE_FOOTER=re.compile(r'(?:page\s+\d+(?:\s+of\s+\d+)?|第\s*\d+\s*页|'
                           r'BankMind Data Lab\s*/\s*\d{4}-\d{2}-\d{2}\s*/\s*全部交易为合成数据\s+\d+)',re.I)

def pdf_cash_layout(lines, page_count):
    pages={}
    for page,line in lines:
        pages.setdefault(page,[]).append(line)
    columns=None; printed={}; rows=[]
    for page, page_lines in pages.items():
        for index,line in enumerate(page_lines[:80]):
            cells=list(PDF_CELL.finditer(line))
            names=[cell.group() for cell in cells]
            mapped=suggest(names)
            if len(names)==len(set(names)) and set(filter(None,mapped.values()))==set(names) and all(
                    mapped[field] for field in ('date','reference','debit','credit','balance')):
                break
        else:
            if columns is None:
                return None
            raise ValueError(f'PDF第{page}页缺少可可靠定位的重复表头')
        if columns is None:
            columns=names
            printed=pdf_printed_controls([(page,line.strip()) for line in page_lines[:index]])
        if names!=columns:
            raise ValueError(f'PDF第{page}页列结构与首页不一致')
        starts=[cell.start() for cell in cells]
        data_lines=page_lines[index+1:]
        previous_count=len(rows)
        for position,line in enumerate(data_lines):
            if position==len(data_lines)-1 and PDF_PAGE_FOOTER.fullmatch(line.strip()):
                continue
            entries=list(PDF_CELL.finditer(line))
            raw=dict.fromkeys(columns,'')
            for cell in entries:
                anchors=[anchor for anchor,start in enumerate(starts) if abs(start-cell.start())<=1]
                if len(anchors)!=1 or raw[columns[anchors[0]]] or (anchors[0]+1<len(starts) and cell.end()>starts[anchors[0]+1]):
                    raise ValueError(f'PDF第{page}页有单元格无法可靠定位，未导入')
                raw[columns[anchors[0]]]=cell.group()
            debit,credit,balance=(raw[mapped[field]] for field in ('debit','credit','balance'))
            valid_balance=PDF_CASH.fullmatch(balance.removeprefix('-'))
            if (not PDF_DATE.fullmatch(raw[mapped['date']]) or not raw[mapped['reference']] or not valid_balance or
                    not (debit or credit) or any(value and not PDF_CASH.fullmatch(value) for value in (debit,credit))):
                raise ValueError(f'PDF第{page}页交易列不完整或金额无法可靠定位，未导入')
            raw['PDF页码']=str(page)
            rows.append({'row_no':len(rows)+1,'raw':raw,'page':page})
            if len(rows)>MAX_ROWS:
                raise ValueError('PDF超过20,000行，请拆分后导入')
        if len(rows)==previous_count:
            raise ValueError(f'PDF第{page}页没有可核对的交易明细')
    if 'transaction_count' in printed and printed['transaction_count']!=len(rows):
        raise ValueError('PDF标注的交易笔数与解析行数不一致，未导入')
    return {'columns':columns,'rows':rows,'sheets':[],'sheet':'','encoding':'pdf-text','pdf_pages':page_count,
            'suggested_mapping':suggest(columns),'printed_statement_controls':printed}

def pdf_header(lines):
    hints=re.compile(r'日期|金额|借方|贷方|摘要|对方|账号|币种|编号|余额|date|amount|debit|credit|reference|counterparty|description',re.I)
    for index,(_,line) in enumerate(lines[:80]):
        tokens=[token for token in re.split(r'\t+|\s{2,}',line) if token]
        if len(tokens)>=3 and len(set(tokens))==len(tokens) and len(hints.findall(line))>=2:
            return index,tokens
    return None,[]

def pdf_printed_controls(lines):
    labels={'transaction_count':r'交易笔数|\btransaction count',
            'receipts':r'收入合计|收款总额|\btotal receipts',
            'payments':r'支出合计|付款总额|\btotal payments',
            'opening_balance':r'期初余额|\bopening balance',
            'closing_balance':r'期末余额|\bclosing balance'}
    controls={}
    for field,label in labels.items():
        number=r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)'
        pattern=number if field=='transaction_count' else r'-?'+number+r'(?:\.[0-9]{1,3})?'
        found={match.replace(',','') for _,line in lines for match in re.findall(
            rf'(?:{label})\s*:\s*({pattern})(?=\s|$)',line,re.I)}
        if len(found)>1:
            raise ValueError('PDF表头前同一控制数出现不同值，无法确认口径，未导入')
        if found:
            value=found.pop()
            controls[field]=int(value) if field=='transaction_count' else value
    return controls

def normalize_row(row, config, task, account):
    raw, mapping = row['raw'], config['fields']
    def value(field):
        return raw.get(mapping.get(field, ''), '')
    result = {'row_no': row['row_no'], 'raw': raw, 'date': '', 'amount': 0, 'currency': '',
              'reference': value('reference'), 'counterparty': value('counterparty'),
              'description': value('description'), 'group_key': value('group_key'), 'issues': []}
    def issue(code, field, message):
        result['issues'].append({'code': code, 'field': field, 'message': message})
    currency = value('currency') if mapping.get('currency') else config.get('default_currency', '')
    result['currency'] = currency.upper()
    if currency.upper() != task.currency:
        issue('CURRENCY_MISMATCH', 'currency', '币种缺失或与任务不一致')
    number = value('account') if mapping.get('account') else config.get('default_account', '')
    if number != account.number:
        issue('ACCOUNT_MISMATCH', 'account', '账号缺失或与授权账户不一致')
    try:
        result['date'] = parse_date(value('date'), config.get('date_format', ''))
        if not task.start_date <= result['date'] <= task.end_date:
            issue('OUTSIDE_PERIOD', 'date', '日期不在任务期间')
    except ValueError as e:
        issue('DATE_INVALID', 'date', str(e))
    try:
        direction = config.get('direction')
        if direction in ['debit_in', 'credit_in']:
            debit, credit = money(value('debit') or '0', task.currency), money(value('credit') or '0', task.currency)
            if debit < 0 or credit < 0 or (debit and credit):
                issue('DIRECTION_INVALID', 'amount', '借贷双列同时非零或负数，需专项复核')
            result['amount'] = debit-credit if direction == 'debit_in' else credit-debit
        else:
            result['amount'] = money(value('amount'), task.currency) * (-1 if direction == 'signed_out' else 1)
        if not result['amount']:
            issue('ZERO_AMOUNT', 'amount', '零额记录隔离至专项复核')
        if re.search(r'退款|冲正|撤销|reversal|refund', result['description'], re.I):
            issue('REVERSAL_REVIEW', 'description', '退款/冲正需专项人工复核，不按绝对额抵销')
    except ValueError as e:
        issue('AMOUNT_INVALID', 'amount', str(e))
    if any('#FORMULA_REJECTED:' in str(v) for v in raw.values()):
        issue('FORMULA_REJECTED', 'raw', '公式单元格不执行；请提供已核验的静态值')
    result['fingerprint'] = sha256(str([result[k] for k in ['date','amount','currency','reference','counterparty','description']]).encode()).hexdigest()
    result['quality'] = 'QUARANTINED' if result['issues'] else 'VALID'
    return result
