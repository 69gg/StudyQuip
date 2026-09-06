# StudyQuip

个人错题与教材学习工作台。作者 **Null** · **pylindex@qq.com** · **MIT License**。

上传教材，校对 AI 整理的正文与目录，再录入错题、确认答案、生成有来源的讲解，最后导出练习或复习 PDF。支持手机上传与拍照、任意目录层级、浅色／深色界面和延迟处理。

## 启动

需要项目固定的 **uv 0.12.10**、**Node.js 22** 和 **pnpm 11.3.0**。Python 固定为 uv 托管 **CPython 3.13.15 / PBS 20260901**，官方构建下载元数据地址在 `pyproject.toml`。第三方 Python 和前端依赖分别锁在 `uv.lock` 和 `frontend/pnpm-lock.yaml`。

在项目根目录执行：

```sh
uv sync --locked --managed-python --python 3.13.15
uv run studyquip doctor
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run playwright install chromium
uv run studyquip init
uv run studyquip run
```

`init` 会检查真实 SQLite 能力、显式迁移，并交互设置至少 8 个字符的登录密码。打开 **http://127.0.0.1:8765**。`run` 同时启动 Web 与唯一 worker，Ctrl+C 一起停止。

需要分开管理进程时，在两个终端运行：

```sh
uv run studyquip web
uv run studyquip worker
```

worker 负责全部模型请求、模型连接测试、后台教材处理和 PDF 渲染。生成 PDF 时 Web 也必须运行，供 Chromium 读取独立的导出页面。

## 使用流程

登录后默认进入首页，可以继续打开最近的题目或教材，查看需要处理的任务，也可以直接录入错题、导入教材。首次使用时，首页根据当前资料与模型配置显示开始步骤。侧栏和手机顶部导航都能返回首页。

1. 在设置中维护科目，添加视觉、讲解与嵌入模型。填入服务商 Base URL、API Key、实际模型 ID 和 API 协议；视觉／讲解角色可以使用同一模型。同角色有多个配置时，当前使用最早配置，删除前请检查运行任务。
2. 在教材页输入书名与科目，粘贴文本或上传 PDF、多张图片／文本文件，调整输入顺序，再选择立即或预约处理。按原始顺序进行正式修订，页草稿识别可并行。
3. 在教材工作台检查目录和正文。目录保留原书名称，允许任意深度；正文可修改、拆分、合并、归档及查看历史。人工修改受保护，AI 的相关修改进入建议队列，采纳前显示差异并校验版本。
4. 遇到识别困难的页面，可以重新识别、手动校正、跳过或明确采用 PDF 可提取文字。跳过会保留缺口，后续 AI 不会跨缺口拼接段落。
5. 录入一道错题，填写或由 AI 整理题干、题型、选项、原作答，上传可选参考解析。正确答案只能由用户填写或从参考资料提取，**必须由用户确认**。备注始终由用户编辑。
   “做错原因”由用户填写，可以留空；“让 AI 优化表述”默认不勾选。勾选且有原文时，在生成讲解的同一次请求中优化表达，原文始终保留，优化结果单独显示。修改错因或开关会使旧讲解待更新，不影响正确答案确认。
6. 选择相关教材；不选时检索同科目全部教材。生成讲解和知识点。引用显示书名和真实目录路径，不显示教材页码。无原文命中时明确显示缺少教材依据。
7. 勾选错题并调整顺序，导出 PDF。练习版只含题目、选项、必要配图和留白；复习版可以包含答案、讲解与知识点。练习配图独立选择，请使用干净的题图，不直接使用带答案批改的原始照片。

第一版没有连续追问聊天界面，也没有多用户数据隔离。

## 模型、时间窗口与并发

DeepSeek V4 开启思考时，工具选择策略设为“不发送（服务商默认）”；最大输出默认留空，若需指定上限，输出长度字段使用 `max_tokens`。需要较高思考强度时填写 `max`。应用仍要求模型通过工具提交结构化结果。[具体协议与参数说明](docs/ai-runtime.md)

视觉和讲解模型支持 Chat Completions、Responses，嵌入模型使用 Embeddings API。所有结构化输出经过工具调用及应用校验。

- 思考扩展分为“不发送 / enabled / disabled”；`thinking` 是特定服务商扩展，使用 OpenAI 官方接口时默认不发送。
- effort 使用服务商实际接受的值；Chat 映射到 `reasoning_effort`，Responses 映射到 `reasoning.effort`。
- 最大输出默认不传，留空时采用服务商默认上限；填写正整数才发送对应参数。已有的数字上限继续生效，清空并保存即可取消。
- 可设置采样参数、超时、重试、工具轮数、上下文预算和扩展 JSON；冲突字段明确报错。
- Responses 默认显式 `store=false`，保留加密推理项和完整工具续接记录；切换为 `store=true` 后按已存储项 ID 引用。
- 每个模型桶默认 4 并发。同凭据、端点、模型的角色／协议共享名额；可选凭据总并发上限叠加在全部模型上。
- 可设置预约时间、延迟及每天多个允许请求时间段，默认 `Asia/Shanghai`、全天。时间窗口按每次请求重新检查；已发出的请求允许结束。时间窗口由用户配置，应用不会自动查询服务商峰谷价格。
- 任务页显示排队原因、阶段、请求用量、失败信息及重试／改期／取消操作。凭据并发上限不等同于 RPM 限制。

详细协议见 [模型与任务运行协议](docs/ai-runtime.md)、[教材修订与证据协议](docs/textbook-protocol.md)。

## 手机与局域网

默认只监听 `127.0.0.1`。需要手机访问时，显式设置监听地址和允许的浏览器来源，例如电脑地址是 `192.168.1.20`：

```sh
STUDYQUIP_HOST=0.0.0.0 STUDYQUIP_ALLOWED_ORIGINS='["http://192.168.1.20:8765"]' uv run studyquip run
```

Windows PowerShell 使用 `$env:STUDYQUIP_HOST='0.0.0.0'` 和 `$env:STUDYQUIP_ALLOWED_ORIGINS='["http://192.168.1.20:8765"]'` 设置同名变量，再运行启动命令；也可参考 `.env.example` 创建 `.env`。

手机打开电脑的局域网地址。拍照使用浏览器文件选择能力，提供单独的相册入口；实际相机交互由手机浏览器决定。图片归正后裁剪，原始文件保留，支持 HEIC/HEIF。

HTTP cookie 使用 `HttpOnly`、`SameSite=Lax`，不设置 `Secure`；修改接口使用 CSRF token 与来源校验。**这套 HTTP 配置仅用于本机或可信局域网，不要暴露公网。**

## 配置与数据

配置由 `STUDYQUIP_` 前缀的环境变量或 `.env` 提供，完整默认值见 `src/studyquip/config.py`。常用项见 [运行与配置](docs/operations.md)。

默认 `data/` 保存 SQLite 数据库、不可变原件、派生图片与 PDF、应用密钥和持久锁文件。模型凭据保存在应用数据库中；该目录不纳入版本库，备份时与教材一起妥善保存。

更改密码使用 `uv run studyquip password`，旧会话会失效。升级前停止 Web 和 worker，备份完整数据目录，再执行 `uv run studyquip upgrade`。禁止删除锁文件来“解锁”，禁止将数据库放在网络共享目录。

## 验证

```sh
uv run pytest -q
uv run ruff check src tests scripts
pnpm --dir frontend build
pnpm --dir frontend test
uv run python scripts/evaluate_retrieval.py
uv run python scripts/verify_local.py
```

自动测试集中覆盖协议续接、两级并发、跨进程锁与租约、一次性操作回执、教材修订／恢复、证据定位及必要用户约束，不设覆盖率目标。

`verify_local.py` 在 `.verification/` 创建独立验收资料，启动真实 Web 和 worker，验证浏览器登录、首页快捷入口与最近记录、移动视口、练习／复习 PDF、中文与公式、配图加载及字体故障；空资料库使用独立浏览器夹具检查。生成截图、PDF 和报告后关闭服务。它不修改默认 `data/`，不调用外部模型。

GitHub Actions 配置了 Windows、macOS、Linux 的运行时预检和关键测试。本机通过不能替代另两平台实际运行；真实手机拍照和真实服务商模型质量也需要对应设备与 API Key。具体覆盖见 [验收记录](docs/validation.md)。

## 字体与许可

应用代码使用 MIT。Noto Sans CJK SC 字体和 KaTeX 字体按 OFL 随应用分发，KaTeX 代码遵循其 MIT 许可。文件、许可及下载校验信息在 `frontend/public/fonts/`；不使用外部字体 CDN，不裁剪字体。PDF 使用固定字体，工作台使用系统字体以减少移动端首次加载体积。

技术选型与实际边界见 [运行与配置](docs/operations.md)、[教材协议](docs/textbook-protocol.md)、[AI 协议](docs/ai-runtime.md)、[小型检索对照](docs/retrieval-evaluation.md)。
