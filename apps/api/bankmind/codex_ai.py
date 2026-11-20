"""Use the local Codex app server with volatile state and a read-only turn."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from .db import STORAGE


def login_tokens():
    home = Path(os.getenv('CODEX_HOME') or Path.home() / '.codex')
    try:
        auth = json.loads((home / 'auth.json').read_text(encoding='utf-8'))
        tokens = auth['tokens']
        access_token, account_id = tokens['access_token'], tokens['account_id']
        if not (access_token and account_id):
            raise KeyError('empty credential')
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('CODEX_LOGIN_REQUIRED') from None
    return {'accessToken': access_token, 'chatgptAccountId': account_id}


async def _run_codex(folder, instructions, evidence, schema):
    command = [os.environ['BANKMIND_CODEX_PATH'], 'app-server', '--stdio',
               '--disable', 'shell_tool', '--disable', 'shell_snapshot', '--disable', 'apps',
               '-c', 'web_search="disabled"']
    if os.name == 'nt':
        command += ['-c', 'windows.sandbox="unelevated"']

    visible = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'USERNAME', 'USERPROFILE',
               'HOME', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA',
               'PROGRAMFILES', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {key: value for key, value in os.environ.items() if key.upper() in visible}
    env.update(CODEX_HOME=folder, TEMP=folder, TMP=folder)
    tokens = login_tokens()
    process = await asyncio.create_subprocess_exec(
        *command, cwd=folder, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=512_000,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )

    async def send(message):
        process.stdin.write(json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n')
        await process.stdin.drain()

    async def receive():
        line = await process.stdout.readline()
        if not line:
            raise ValueError('CODEX_EXEC_FAILED')
        return json.loads(line)

    async def request(id, method, params):
        await send({'id': id, 'method': method, 'params': params})
        while True:
            message = await receive()
            if 'method' not in message and message.get('id') == id:
                if 'error' in message:
                    raise ValueError('CODEX_LOGIN_REQUIRED' if id == 2 else 'CODEX_EXEC_FAILED')
                return message['result']

    try:
        await request(1, 'initialize', {'clientInfo': {'name': 'bankmind', 'version': '1.0'},
                                       'capabilities': {'experimentalApi': True}})
        await send({'method': 'initialized'})
        await request(2, 'account/login/start', {'type': 'chatgptAuthTokens', **tokens})
        thread = await request(3, 'thread/start', {
            'cwd': folder, 'ephemeral': True, 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'developerInstructions': '仅分析附带数据。不要读写文件、执行命令或调用任何工具。'
        })
        prompt = instructions + '\n输入数据：' + json.dumps(evidence, ensure_ascii=False)
        await request(4, 'turn/start', {
            'threadId': thread['thread']['id'], 'input': [{'type': 'text', 'text': prompt}],
            'outputSchema': schema
        })
        reply = ''
        while True:
            message = await receive()
            method, params = message.get('method'), message.get('params', {})
            if method == 'account/chatgptAuthTokens/refresh':
                try:
                    current = login_tokens()
                except ValueError:
                    raise ValueError('CODEX_AUTH_EXPIRED') from None
                if current == tokens:
                    raise ValueError('CODEX_AUTH_EXPIRED')
                tokens = current
                await send({'id': message['id'], 'result': tokens})
            elif 'id' in message and method:
                # Reject tool execution and approvals from the model.
                await send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unavailable'}})
            elif method == 'item/completed' and params['item']['type'] == 'agentMessage':
                reply = params['item']['text']
            elif method == 'turn/completed':
                if params['turn']['status'] != 'completed' or not reply:
                    raise ValueError('CODEX_EXEC_FAILED')
                if len(reply) > 100_000:
                    raise ValueError('OUTPUT_TOO_LARGE')
                return json.loads(reply)
    finally:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def run_codex(instructions, evidence, schema):
    with TemporaryDirectory(dir=STORAGE, prefix='codex-') as directory:
        return asyncio.run(asyncio.wait_for(_run_codex(directory, instructions, evidence, schema), timeout=85))
