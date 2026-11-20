import os
import platform
import ssl  # preload OpenSSL DLLs before other native modules
if os.name == 'nt' and hasattr(platform, '_wmi_query'):
    platform._wmi_query = lambda *a, **k: (_ for _ in ()).throw(OSError('WMI disabled'))
for _proxy_var in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
    os.environ.pop(_proxy_var, None)
os.environ['NO_PROXY'] = '*'
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest

test_root=Path(__file__).resolve().parents[1]/'storage'/'tests'
test_root.mkdir(parents=True, exist_ok=True)
test_directory=TemporaryDirectory(prefix='run-', dir=test_root)
test_storage=Path(test_directory.name)
postgres_url=os.getenv('BANKMIND_TEST_POSTGRES_URL','')
if postgres_url:
    from sqlalchemy.engine import make_url
    configured=make_url(postgres_url)
    if configured.drivername!='postgresql+psycopg' or not (configured.database or '').startswith('bankmind_test_'):
        raise RuntimeError('PostgreSQL tests require a dedicated bankmind_test_* database')
os.environ['DATABASE_URL']=postgres_url or 'sqlite:///'+str(test_storage/'test.db')
os.environ['BANKMIND_STORAGE']=str(test_storage)
os.environ['BANKMIND_DEMO']='true'
os.environ['LLM_MODE']='mock'
os.environ['LLM_ALLOW_EXTERNAL']='false'
os.environ['DOCUMENT_AI_ENABLED']='false'
os.environ['DOCUMENT_AI_API_KEY']=''
os.environ['WECHAT_APP_ID']=''
os.environ['WECHAT_APP_SECRET']=''
os.environ['BANKMIND_PUBLIC_TRIAL']='false'


def pytest_sessionfinish(session, exitstatus):
    from bankmind.db import engine
    engine.dispose()
    test_directory.cleanup()
    try:
        test_root.rmdir()
    except OSError:
        pass

@pytest.fixture(scope='session')
def client():
    from fastapi.testclient import TestClient
    from bankmind.main import app
    with TestClient(app) as c:yield c

@pytest.fixture(scope='session')
def auth(client):
    result={}
    for username in ['admin','accountant','reviewer','auditor','other']:
        r=client.post('/api/auth/login',json={'username':username,'password':'BankMind@2026'})
        assert r.status_code==200,r.text
        result[username]={'Authorization':'Bearer '+r.json()['token']}
    return result
