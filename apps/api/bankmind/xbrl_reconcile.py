"""Lightweight XBRL / iXBRL Cash & Cash Equivalents reconciliation module.

Strictly enforces accounting scope comparability:
- XBRL reports general-ledger / financial-statement totals (e.g., Cash & Cash Equivalents),
  NOT individual bank transactions.
- Cash & Cash Equivalents may include petty cash, other monetary funds, restricted cash,
  multiple bank accounts, or consolidation scope differences.
- Unless currency, period instant, and accounting scope are all confirmed comparable,
  this module reports NOT_COMPARABLE ('口径待核对 / 暂不可勾稽') rather than falsely
  claiming an undisclosed discrepancy.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as StdET

try:
    from defusedxml import ElementTree as SafeET
except ImportError:
    SafeET = None

try:
    from lxml import html as lxml_html
except ImportError:
    lxml_html = None

CASH_CONCEPTS = {
    'cashandcashequivalentsatcarryingvalue': ('US-GAAP', '现金及现金等价物'),
    'cashcashequivalentsrestrictedcashandrestrictedcashequivalents': ('US-GAAP', '现金、现金等价物及受限资金'),
    'cash': ('US-GAAP', '现金'),
    'cashandcashequivalents': ('IFRS', '现金及现金等价物'),
    'monetaryfunds': ('CAS', '货币资金'),
    'cashandbankbalances': ('CAS', '货币资金（现金及银行存款）'),
    '货币资金': ('CAS', '货币资金'),
}


def _local_name(tag: str) -> str:
    if not tag:
        return ''
    if '}' in tag:
        tag = tag.split('}', 1)[1]
    if ':' in tag:
        tag = tag.split(':', 1)[1]
    return tag.strip()


def _parse_decimal(raw: str, scale: int = 0, negative: bool = False) -> Decimal | None:
    cleaned = re.sub(r'[,\s¥￥$€]', '', str(raw or '').strip())
    if cleaned.startswith('(') and cleaned.endswith(')'):
        negative = True
        cleaned = cleaned[1:-1]
    if not cleaned or cleaned in ('-', '—'):
        return None
    try:
        val = Decimal(cleaned)
        if scale:
            val = val * (Decimal(10) ** scale)
        return -abs(val) if negative else val
    except InvalidOperation:
        return None


def _safe_xml_fromstring(content: bytes):
    if SafeET is not None:
        return SafeET.fromstring(content)
    if b'<!ENTITY' in content.upper() or b'<!DOCTYPE' in content.upper():
        raise ValueError('XBRL 包含禁用 XML 声明（DOCTYPE/ENTITY），已拒绝解析')
    return StdET.fromstring(content)


def _extract_ixbrl_regex(text: str) -> tuple[dict[str, dict], dict[str, str], list[dict]]:
    contexts: dict[str, dict] = {}
    units: dict[str, str] = {}
    facts: list[dict] = []

    for m in re.finditer(r'<(?:\w+:)?context\b[^>]*\bid=["\']([^"\']+)["\'][^>]*>(.*?)</(?:\w+:)?context>', text, re.I | re.S):
        cid, body = m.group(1), m.group(2)
        im = re.search(r'<(?:\w+:)?(?:instant|endDate)>([^<]+)</', body, re.I)
        contexts[cid] = {'instant': im.group(1).strip() if im else ''}

    for m in re.finditer(r'<(?:\w+:)?unit\b[^>]*\bid=["\']([^"\']+)["\'][^>]*>(.*?)</(?:\w+:)?unit>', text, re.I | re.S):
        uid, body = m.group(1), m.group(2)
        mm = re.search(r'<(?:\w+:)?measure>([^<]+)</', body, re.I)
        if mm:
            units[uid] = mm.group(1).strip().split(':')[-1].upper()

    for m in re.finditer(r'<(?:\w+:)?nonFraction\b([^>]*)>(.*?)</(?:\w+:)?nonFraction>', text, re.I | re.S):
        attrs_s, inner = m.group(1), re.sub(r'<[^>]+>', '', m.group(2))
        attrs = {k.lower(): v for k, v in re.findall(r'([\w:-]+)=["\']([^"\']*)["\']', attrs_s)}
        name = attrs.get('name', '')
        local = _local_name(name).lower()
        if local not in CASH_CONCEPTS:
            continue
        scale_raw = attrs.get('scale', '0')
        scale = int(scale_raw) if re.match(r'^-?\d+$', scale_raw) else 0
        negative = attrs.get('sign', '').strip() == '-'
        val = _parse_decimal(inner, scale=scale, negative=negative)
        if val is None:
            continue
        cid = attrs.get('contextref', '')
        uid = attrs.get('unitref', '')
        taxonomy, zh_label = CASH_CONCEPTS[local]
        facts.append({
            'concept': name,
            'taxonomy': taxonomy,
            'label': zh_label,
            'amount': str(val.quantize(Decimal('0.01'))),
            'currency': units.get(uid, ''),
            'instant': contexts.get(cid, {}).get('instant', ''),
            'context_ref': cid,
            'source_format': 'iXBRL',
        })
    return contexts, units, facts


def extract_xbrl_cash_facts(content: bytes) -> dict:
    """Extract cash balance facts from XBRL (.xml/.xbrl) or iXBRL (.htm/.xhtml)."""
    if not content:
        raise ValueError('XBRL 文件为空')
    head = content[:2048].lstrip().lower()
    is_ixbrl = b'<html' in head or b'<!doctype html' in head or b'nonfraction' in content[:65536].lower()

    contexts: dict[str, dict] = {}
    units: dict[str, str] = {}
    facts: list[dict] = []

    if is_ixbrl:
        if lxml_html is not None:
            parser = lxml_html.HTMLParser(remove_comments=True, encoding='utf-8')
            doc = lxml_html.fromstring(content, parser=parser)
            for ctx in doc.xpath('//*[local-name()="context"]'):
                cid = ctx.get('id') or ''
                inst = ''.join(ctx.xpath('.//*[local-name()="instant"]/text()')).strip()
                end_dt = ''.join(ctx.xpath('.//*[local-name()="endDate"]/text()')).strip()
                if cid:
                    contexts[cid] = {'instant': inst or end_dt}
            for unit in doc.xpath('//*[local-name()="unit"]'):
                uid = unit.get('id') or ''
                measure = ''.join(unit.xpath('.//*[local-name()="measure"]/text()')).strip()
                if uid and measure:
                    units[uid] = measure.split(':')[-1].upper()
            for el in doc.xpath('//*[local-name()="nonfraction" or local-name()="nonFraction"]'):
                name = el.get('name') or ''
                local = _local_name(name).lower()
                if local not in CASH_CONCEPTS:
                    continue
                scale_raw = el.get('scale') or '0'
                scale = int(scale_raw) if re.match(r'^-?\d+$', scale_raw) else 0
                negative = (el.get('sign') or '').strip() == '-'
                val = _parse_decimal(''.join(el.itertext()), scale=scale, negative=negative)
                if val is None:
                    continue
                cid = el.get('contextref') or el.get('contextRef') or ''
                uid = el.get('unitref') or el.get('unitRef') or ''
                taxonomy, zh_label = CASH_CONCEPTS[local]
                facts.append({
                    'concept': name,
                    'taxonomy': taxonomy,
                    'label': zh_label,
                    'amount': str(val.quantize(Decimal('0.01'))),
                    'currency': units.get(uid, ''),
                    'instant': contexts.get(cid, {}).get('instant', ''),
                    'context_ref': cid,
                    'source_format': 'iXBRL',
                })
        else:
            text = content.decode('utf-8', errors='replace')
            contexts, units, facts = _extract_ixbrl_regex(text)
    else:
        try:
            root = _safe_xml_fromstring(content)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('XBRL XML 解析失败，请确认文件格式合法') from exc

        for el in root.iter():
            ln = _local_name(el.tag).lower()
            if ln == 'context':
                cid = el.attrib.get('id', '')
                inst = ''
                for child in el.iter():
                    cln = _local_name(child.tag).lower()
                    if cln in ('instant', 'enddate') and child.text:
                        inst = child.text.strip()
                        break
                if cid:
                    contexts[cid] = {'instant': inst}
            elif ln == 'unit':
                uid = el.attrib.get('id', '')
                for child in el.iter():
                    if _local_name(child.tag).lower() == 'measure' and child.text:
                        units[uid] = child.text.strip().split(':')[-1].upper()
                        break

        for el in root.iter():
            ln = _local_name(el.tag).lower()
            if ln not in CASH_CONCEPTS:
                continue
            val = _parse_decimal(el.text or '')
            if val is None:
                continue
            cid = el.attrib.get('contextRef', '')
            uid = el.attrib.get('unitRef', '')
            taxonomy, zh_label = CASH_CONCEPTS[ln]
            facts.append({
                'concept': _local_name(el.tag),
                'taxonomy': taxonomy,
                'label': zh_label,
                'amount': str(val.quantize(Decimal('0.01'))),
                'currency': units.get(uid, ''),
                'instant': contexts.get(cid, {}).get('instant', ''),
                'context_ref': cid,
                'source_format': 'XBRL',
            })

    if not facts:
        raise ValueError('XBRL/iXBRL 文档中未找到货币资金（CashAndCashEquivalents / MonetaryFunds）事实标签')

    facts.sort(key=lambda f: f.get('instant') or '', reverse=True)
    return {
        'source_format': 'iXBRL' if is_ixbrl else 'XBRL',
        'facts': facts,
    }


def reconcile_xbrl_balance(
    facts: list[dict],
    task_currency: str,
    task_end_date: str,
    book_closing_balance: str | None,
    scope_confirmed: bool = False,
) -> dict:
    """Compare XBRL reported cash fact against task closing balance with strict scope guards."""
    chosen = next(
        (f for f in facts if f.get('instant') == task_end_date and (not f.get('currency') or f.get('currency') == task_currency)),
        facts[0],
    )
    reasons: list[str] = []
    if not book_closing_balance:
        reasons.append('当前对账任务尚未录入企业账簿期末余额，无法比对')
    if chosen.get('currency') and chosen['currency'] != task_currency:
        reasons.append(f"币种不一致（XBRL 为 {chosen['currency']}，任务为 {task_currency}）")
    if chosen.get('instant') and chosen['instant'] != task_end_date:
        reasons.append(f"资产负债表日不一致（XBRL 为 {chosen['instant']}，任务期末为 {task_end_date}）")
    if not scope_confirmed:
        reasons.append('财报货币资金可能包含其他银行账户、库存现金、受限资金或合并范围差异，需先确认核算口径一致后再勾稽')

    if reasons:
        return {
            'status': 'NOT_COMPARABLE',
            'status_label': '口径待核对 / 暂不可勾稽',
            'fact': chosen,
            'book_closing_balance': book_closing_balance,
            'delta': None,
            'reasons': reasons,
        }

    xbrl_val = Decimal(chosen['amount'])
    book_val = Decimal(book_closing_balance)
    delta = (book_val - xbrl_val).quantize(Decimal('0.01'))
    matched = delta == Decimal('0.00')
    return {
        'status': 'MATCHED' if matched else 'DIFFERENCE',
        'status_label': '财报余额勾稽一致' if matched else '存在财报口径差异，需进一步查证',
        'fact': chosen,
        'book_closing_balance': str(book_val.quantize(Decimal('0.01'))),
        'delta': str(delta),
        'reasons': [] if matched else [f'账簿期末余额与 XBRL 披露货币资金相差 {delta} {task_currency}'],
    }
