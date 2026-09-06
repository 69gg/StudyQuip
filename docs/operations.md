# 运行、配置与恢复

## 运行时与初始化

`studyquip doctor` 使用当前实际解释器，输出 Python 路径／版本、平台／架构、SQLite 版本及 source ID，实际创建 FTS5 并查询，实际加载 sqlite-vec 并计算向量距离，检查共享锁后端。SQLite 基线为 3.51.3，预检失败不会创建业务数据库。

初始固定 uv 0.12.10、CPython 3.13.15、PBS 20260901。下载元数据来自固定 uv tag，构建依赖 SQLite 3.53.1；仍必须对下载后的实际解释器执行 doctor。Windows 通过 portalocker[win32] 引入 pywin32 的共享锁实现。

CI 配置包含 Linux、Windows、macOS 的运行时与后端测试，但配置矩阵不等于已经取得三平台运行结果。锁定的 sqlite-vec 0.1.9 提供 Windows x64、macOS x64／ARM64 和 Linux glibc x64／ARM64 wheel；原生 Windows ARM64 不在当前预编译依赖覆盖内。锁定的 PDFium wheel 要求 macOS 13 或更新版本，部分 macOS Intel 依赖可能需要本机编译工具链。实际安装和 doctor 结果是最终判断依据。

若预检失败：

1. 检查 `uv --version`，按原安装来源安装项目固定版本。
2. `uv python install --reinstall 3.13.15`，固定下载元数据配置不要省略。
3. `uv sync --locked --managed-python --python 3.13.15`。
4. `uv run studyquip doctor`，保留实际解释器路径、架构、SQLite source ID 和错误信息。

不要仅升级系统 sqlite3 CLI。扩展无法加载时，检查当前架构是否有对应 wheel，以及当前解释器是否允许加载扩展；禁止静默切换数据库驱动。Chromium 安装使用 `uv run playwright install chromium`，Linux 缺少动态库时按 Playwright 对该系统的安装说明补齐；不能把其他发行版的包管理命令直接套用。

## 配置

| 环境变量后缀 | 默认 | 用途 |
|---|---|---|
| DATA_DIR | data | 本机数据目录 |
| HOST / PORT | 127.0.0.1 / 8765 | 监听地址 |
| ALLOWED_ORIGINS | [] | 显式额外来源，JSON 数组；本机默认地址始终允许 |
| LEASE_SECONDS / HEARTBEAT_SECONDS | 90 / 15 | 在途任务租约及续租间隔 |
| BUSY_TIMEOUT_MS | 5000 | SQLite 锁等待 |
| MAX_ACTIVE_JOBS | 8 | worker 同时在途任务数量，区别于模型请求并发 |
| WORKER_POLL_SECONDS | 1 | 队列轮询间隔 |
| MAX_UPLOAD_MB / MAX_PDF_PAGES | 100 / 2000 | 单文件及 PDF 页数限制 |
| MAX_IMAGE_PIXELS | 80000000 | 单图像／渲染像素限制 |
| CONTEXT_TOKENS / OUTPUT_TOKENS | 24000 / 4096 | 默认处理预算；模型配置另有边界 |
| PDF_TIMEOUT_SECONDS / PDF_SCALE | 120 / 2 | PDF 准备超时与识别图像渲染比例 |
| DIRECTORY_CANDIDATES / GLOBAL_CANDIDATES | 4 / 20 | 目录／全文候选量 |
| SUBTREE_CANDIDATES / FINAL_BLOCKS | 6 / 12 | 子树补充／最终原文块 |
| RRF_K | 60 | 多通道融合常数 |
| DENSE_WEIGHT / LEXICAL_WEIGHT | 1 / 1 | 向量／关键词权重 |
| DIRECTORY_WEIGHT / RELATION_WEIGHT | 0.5 / 0.5 | 目录／关系权重 |
| RELATION_HOPS | 2 | 概念关系展开，不限制目录层数 |
| BLOCK_RELATION_LIMIT / CONCEPT_RELATION_LIMIT / NODE_RELATION_LIMIT | 3 / 6 / 12 | 当前有效非结构关系数量 |
| SUMMARY_BATCH_SIZE | 8 | 节点概述批量上限 |

全部变量添加 `STUDYQUIP_` 前缀。模型的窗口、时区、两级并发、输出长度和额外处理预算在 Web UI 配置；同模型桶的冲突并发上限取较小值。

## 数据库和迁移

Web 和 worker 在生命周期内持有 `.schema.lock` 共享锁，迁移持独占锁；worker 另持 `.worker.lock` 独占锁。锁文件长期保留，不截断、不删除或替换。只支持本机文件系统。

写 engine 显式 BEGIN IMMEDIATE，读 engine 使用 mode=ro、query_only=ON 和普通 BEGIN。模型网络、图片处理和 PDF 渲染都不占写事务。每次连接配置 foreign_keys、busy_timeout 和 synchronous=FULL。SQLite WAL 在初始化／升级侧设置。

升级流程：停止两个进程 → 备份完整 data 目录 → `uv run studyquip upgrade` → `uv run studyquip doctor` → 启动应用。运行进程不自动迁移。

`studyquip run` 在 POSIX 系统向子进程发送 SIGTERM；Windows 将 Web 和 worker 放在独立进程组，以 CTRL_BREAK_EVENT 请求清理，worker 处理 SIGBREAK。不能将 Windows 的 `Popen.terminate()` 当作可拦截的 SIGTERM：它调用 TerminateProcess，见 [Python 子进程文档](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.terminate)。控制台事件不可用或子进程未在退出时限内结束时，启动器才使用强制终止兜底；正常使用请在启动终端按 Ctrl+C 停止。

## 恢复与任务行为

任务通过数据库时钟认领，每次拥有新的 lease_token。每个在途任务独立心跳；模型、图片／PDF 子进程及等待本书顺序锁期间持续续租。续租失败会取消本次逻辑流程，迟到结果不能提交。业务结果、检查点和完成状态共享 fencing 短事务。

重启会重领过期任务并使用已保存检查点。原件和成功提交的正文版本保留；网络断线可能使远端请求重复计费，数据库幂等不能提供远端 exactly-once。

页面需要人工处理时，该书正式顺序修订暂停，其他教材继续。修复／跳过后恢复最早等待中的同书任务；已有可运行同书任务时不重复创建恢复任务。修改输入版本使旧任务停止提交；应基于新输入创建任务。

普通正文恢复使用新操作组，生成新版本；越过合并／拆分拓扑的恢复必须包含所有关联块，不能单独复活被合并 ID。具体协议见 textbook-protocol.md。

备份恢复时整体还原数据库、原件／派生文件和 `.secret`。不要只复制正在写入的单个 sqlite3 文件；停止应用后再复制完整目录。保留原备份，恢复副本通过 doctor 和内容抽查后再切换使用。

## HTTP 与开发

默认只监听 127.0.0.1。HTTP cookie 不设置 Secure，设置 HttpOnly、SameSite=Lax；修改请求同时需要会话 CSRF token，并检查浏览器来源。只用于本机或可信局域网，不暴露公网。

开发模式可运行 `pnpm --dir frontend dev`。Vite 将 /api 代理到 127.0.0.1:8765，设置 `VITE_API_TARGET` 可更改目标。后端 ALLOWED_ORIGINS 需加入实际 Vite 来源，例如 `http://127.0.0.1:5173`；生产使用构建后的同源静态文件。

PDF 使用独立限时快照 token，只能读取该快照及其配图。输出文件按 snapshot/attempt 隔离，任务成功提交后才发布下载地址。练习版在服务端强制关闭答案、讲解和知识点，客户端也按模式限制显示。

字体加载、配图文件或配图记录缺失会使导出报错；不会静默忽略缺失配图后生成文件。
