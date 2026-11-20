import os
import subprocess
import sys
import time
from pathlib import Path
import httpx
import pytest
from test_pdf_single_side import create_task, import_csv, text_pdf
from bankmind.db import STORAGE


@pytest.mark.parametrize(('settings', 'error'), [
    ({'BANKMIND_TRIAL_DEEPSEEK_ENABLED': 'false', 'BANKMIND_DEEPSEEK_KEY': ''}, None),
    ({}, None),
    ({'BANKMIND_TRIAL_DEEPSEEK_ENABLED': 'false'}, 'BANKMIND_TRIAL_DEEPSEEK_ENABLED=true'),
    ({'BANKMIND_DEEPSEEK_MODEL': ''}, 'BANKMIND_DEEPSEEK_MODEL'),
    ({'BANKMIND_DEEPSEEK_KEY': 'LLM_API_KEY=synthetic-key'}, 'only the API key'),
    ({'BANKMIND_DEEPSEEK_URL': 'LLM_BASE_URL=https://api.deepseek.com'}, 'HTTPS base URL'),
    ({'BANKMIND_DEEPSEEK_URL': 'https://api.deepseek.com/chat/completions'}, 'HTTPS base URL'),
    ({'LLM_ALLOW_EXTERNAL': 'true'}, 'generic external AI'),
    ({'LLM_MODE': 'live'}, 'LLM_MODE=mock'),
    ({'DOCUMENT_AI_ENABLED': 'true'}, 'document AI'),
    ({'BANKMIND_CODEX_ENABLED': 'true'}, 'Codex disabled'),
    ({'WECHAT_APP_SECRET': 'synthetic-secret'}, 'enterprise-code login'),
])
def test_public_trial_deepseek_deployment(monkeypatch, settings, error):
    from bankmind.operations import validate_deployment

    defaults = {
        'BANKMIND_PUBLIC_TRIAL': 'true', 'BANKMIND_DEMO': 'true', 'BANKMIND_PILOT': 'false',
        'BANKMIND_WORK_MODE': 'embedded', 'BANKMIND_TRIAL_PASSWORD': 'synthetic-reviewer-password',
        'BANKMIND_TRIAL_ACCOUNTANT_PASSWORD': '', 'BANKMIND_TRIAL_AUDITOR_PASSWORD': '',
        'LLM_MODE': 'mock', 'LLM_ALLOW_EXTERNAL': 'false', 'DOCUMENT_AI_ENABLED': 'false',
        'BANKMIND_CODEX_ENABLED': 'false', 'WECHAT_APP_SECRET': '',
        'BANKMIND_TRIAL_DEEPSEEK_ENABLED': 'true', 'BANKMIND_DEEPSEEK_KEY': 'synthetic-key',
        'BANKMIND_DEEPSEEK_URL': 'https://api.deepseek.com', 'BANKMIND_DEEPSEEK_MODEL': 'deepseek-flash',
    }
    for name, value in {**defaults, **settings}.items():
        monkeypatch.setenv(name, value)
    if error is None:
        validate_deployment()
    else:
        with pytest.raises(RuntimeError, match=error):
            validate_deployment()


@pytest.mark.parametrize(('finish', 'content', 'expected'), [
    ('stop', '```json\n{"status":"ok"}\n```', {'status': 'ok'}),
    ('stop', ' ', 'EMPTY_MODEL_OUTPUT'),
    ('length', '{"status":', 'OUTPUT_TRUNCATED'),
])
def test_deepseek_structured_output_messages(monkeypatch, finish, content, expected):
    from bankmind.ai import OpenAICompatibleProvider

    for key, value in {
        'KEY': 'synthetic-key', 'URL': 'https://api.deepseek.com', 'MODEL': 'deepseek-flash'
    }.items():
        monkeypatch.setenv('BANKMIND_DEEPSEEK_' + key, value)

    def sample_reply(self, url, *, headers, json):
        assert json['thinking'] == {'type': 'disabled'}
        assert json['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, request=httpx.Request('POST', url),
                              json={'choices': [{'finish_reason': finish, 'message': {'content': content}}]})

    monkeypatch.setattr(httpx.Client, 'post', sample_reply)
    provider = OpenAICompatibleProvider('deepseek')
    evidence = {'records': [{'id': 'synthetic'}]}
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=expected):
            provider.investigate(evidence)
    else:
        assert provider.investigate(evidence) == expected


def test_mobile_report_requires_source_and_independent_signer(client, auth):
    accountant, reviewer = auth['accountant'], auth['reviewer']
    task = create_task(client, auth, '移动端签署合成验收')
    path = '/api/tasks/' + task['id']
    for side in ('BANK', 'BOOK'):
        _, result = import_csv(client, auth, task, side,
                               '2026-10-05,23.45,0000000000006688,CNY,MOB-101,合成对方,合成业务')
        assert result.status_code == 200, result.text
    run = client.post(path + '/run', headers=accountant, json={})
    assert run.status_code == 200, run.text
    fresh = client.get(path, headers=accountant).json()
    values = {'opening': '10.00', 'closing': '33.45'}
    balance = client.patch(path + '/balances', headers=accountant, json={
        'version': fresh['version'], 'BANK': values, 'BOOK': values, 'reason': '核对合成原件余额'
    })
    assert balance.status_code == 200, balance.text
    sides = {'rows': 1, 'net_amount': '23.45'}
    source = client.post(path + '/source-controls', headers=accountant, json={
        'BANK': sides, 'BOOK': sides, 'source_reference': 'MOB-CTRL',
        'statement': '两侧独立合成原件的账户、期间和控制数已核对',
    })
    assert source.status_code == 200, source.text
    statement = source.json()
    review = client.post('/api/source-controls/' + statement['id'] + '/decision', headers=reviewer, json={
        'action': 'APPROVE', 'version': statement['version'], 'reason': '独立核对来源和金额',
        'idempotency_key': 'mobile-source-control-check',
    })
    assert review.status_code == 200, review.text
    assert review.json()['status'] == 'VERIFIED'
    draft = client.post(path + '/reports?reuse_current=true', headers=accountant)
    assert draft.status_code == 200, draft.text
    report = draft.json()
    sign_path = '/api/reports/' + report['id'] + '/sign'
    decision = {'action': 'APPROVE', 'version': report['version'], 'reason': '独立核对完整合成证据',
                'idempotency_key': 'mobile-independent-sign-check'}
    assert client.post(sign_path, headers=accountant, json=decision).status_code == 403
    signed = client.post(sign_path, headers=reviewer, json=decision)
    assert signed.status_code == 200, signed.text
    assert signed.json()['status'] == 'SIGNED'


def test_model_probe_and_investigation_permissions(client, auth, monkeypatch):
    for key in ('BANKMIND_CODEX_PATH', 'BANKMIND_CODEX_ENABLED', 'BANKMIND_DEEPSEEK_KEY'):
        monkeypatch.delenv(key, raising=False)
    reviewer = auth['reviewer']
    assert client.post('/api/ai/probe/codex', headers=reviewer).status_code == 400
    unavailable = client.post('/api/ai/probe/deepseek?confirm_external=true', headers=reviewer)
    assert unavailable.json()['error'] == 'PROVIDER_NOT_CONFIGURED'
    assert client.post('/api/ai/probe/mock', headers=auth['auditor']).status_code == 403

    case = next(item for item in client.get('/api/cases', headers=reviewer).json()['items']
                if item['status'] != 'CLOSED')
    path = '/api/cases/' + case['id']
    before = client.get(path, headers=reviewer).json()['version']
    assert client.post(path + '/investigate?provider=codex', headers=reviewer).status_code == 400
    response = client.post(path + '/investigate?provider=deepseek&confirm_external=true', headers=reviewer)
    assert response.status_code == 503
    assert client.get(path, headers=reviewer).json()['version'] == before

def test_native_miniprogram_pages_against_shared_api(client):
    root=Path(__file__).resolve().parents[1]
    statement=STORAGE/'synthetic-mobile-statement.pdf'
    statement.write_bytes(text_pdf(['date  amount  reference','2026-10-04  1.00  R0']))
    env={**os.environ,'BANKMIND_TEST_API':'http://127.0.0.1:8001/api','BANKMIND_WORK_MODE':'embedded',
         'BANKMIND_TEST_MOBILE_STATEMENT':str(statement)}
    server=subprocess.Popen([sys.executable,'-m','uvicorn','bankmind.main:app','--app-dir','apps/api','--host','127.0.0.1','--port','8001'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                # Local loopback checks must ignore corporate/proxy environment
                # variables; some proxy URL patterns are not valid httpx URLs.
                if httpx.get('http://127.0.0.1:8001/api/health',timeout=1,trust_env=False).status_code==200:break
            except (httpx.HTTPError,httpx.InvalidURL):time.sleep(.1)
        result=subprocess.run(['node','tests/miniprogram.test.cjs'],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        assert result.returncode==0,result.stdout+result.stderr
        assert '"passed":true' in result.stdout
    finally:
        server.terminate();server.wait(timeout=10)
