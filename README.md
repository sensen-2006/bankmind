# BankMind 银账智核

微信原生小程序与 FastAPI 后端。支持从合成样本开始体验银行流水导入、对账、异常调查、人工复核和报告。仓库根目录的 Dockerfile 用于构建微信云托管后端。

## 微信云托管

在现有服务的「部署发布」中选择「绑定 GitHub 仓库」。授权后选择以下构建配置。

| 选项 | 值 |
| --- | --- |
| 仓库 | `sensen-2006/bankmind` |
| 分支 | `main` |
| 端口 | `8000` |
| 目标目录 | 留空，使用仓库根目录 |
| Dockerfile 文件 | 有 |
| Dockerfile 名称 | `Dockerfile` |

更新已发布的体验服务时，沿用现有的体验账号设置及 `/app/storage` 存储配置。默认使用合成演示库和 Mock 规则。

如需在体验版启用 DeepSeek，在微信云托管的环境变量填写以下配置。

| 变量 | 值 |
| --- | --- |
| `BANKMIND_TRIAL_DEEPSEEK_ENABLED` | `true` |
| `BANKMIND_DEEPSEEK_KEY` | 供应商发放的 API 密钥本身 |
| `BANKMIND_DEEPSEEK_URL` | HTTPS 基础地址，后端会追加 `/chat/completions` |
| `BANKMIND_DEEPSEEK_MODEL` | 与密钥、基础地址对应的模型名 |

`LLM_MODE` 保持 `mock`。发布后用现有体验版在「我的 → 服务连接 → DeepSeek」测试连接。财务和审核角色可在未关闭的异常详情选择 DeepSeek 生成调查建议。API 密钥由云端后端读取，体验成员使用各自的体验账号。密钥到期后，在云托管更新对应变量并让配置在服务版本中生效。

## 本机联调

使用 Python 3.12，在仓库根目录运行以下 PowerShell 命令。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-team.txt
.\.venv\Scripts\python.exe -m uvicorn bankmind.main:app --app-dir apps/api --host 127.0.0.1 --port 8026
```

在微信开发者工具导入仓库根目录。模拟器连接本机端口 `8026`；真机使用 `apps/miniprogram/config.js` 指定的云托管服务，需有对应的小程序与环境权限。本机 API 文档在 `http://127.0.0.1:8026/docs`。本机如需接入 DeepSeek，可参考 `.env.example` 创建自己的 `.env`，启动命令追加 `--env-file .env`。

测试使用隔离的合成数据库。安装 `requirements-dev.txt` 和 Node.js 后，在仓库根目录运行 `python -m pytest`。

## 素材署名

小程序内的影片画面取自 Blender Studio 的《Spring》，© Blender Foundation，导演 Andy Goralczyk。此处使用影片中提取的静帧，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 保留署名。 [Blender Studio 影片及许可说明](https://studio.blender.org/projects/spring/pages/about/)
