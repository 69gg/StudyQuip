# 模块与接口约定

本文件记录模块之间的实际接口；运行方式、教材协议与模型协议分别见 README、operations.md、textbook-protocol.md 和 ai-runtime.md。

## 后端公共基础

包：`studyquip`，源码位于 `src/studyquip`。所有 Python 函数添加类型注释。

`config.Settings` 为 Pydantic Settings：data_dir(Path)、host、port、allowed_origins(list[str])、lease_seconds=90、heartbeat_seconds=15、busy_timeout_ms=5000、context_tokens=24000、output_tokens=4096、max_upload_mb=100、worker_poll_seconds=1、frontend_dir(Path)。属性 db_path、files_dir。`Settings()` 读 STUDYQUIP_ 环境变量。

`db.Database(settings)` 持有 rw/ro SQLAlchemy engine。`write()` 是返回 Connection 的短事务 context manager；`read()` 是只读 Connection context manager。`get(kind, id, conn=None)` 返回 dict 或 None；`list(kind, *, filters=None, limit=1000, offset=0, conn=None)` 返回 dict 列表，filters 对 data JSON 顶层字段精确比较。`put(kind, data, *, id=None, expected_revision=None, conn=None)` 返回带 id/revision/created_at/updated_at 的 dict；存在时未提供 expected_revision 视为调用方已在同一写事务锁定，外部修改必须明确传版本。`delete(kind,id,*,conn=None)`。`history(kind,id,conn=None)` 返回旧/现版本列表。`secret()` 返回持久化 bytes。`ConflictError` 表示版本冲突。records 表为(kind,id,revision,data JSON,created_at,updated_at)，history 保存每次版本。

数据库初始化集中在 `db.initialize(settings)`，先 doctor 再独占迁移锁，Alembic 创建公共 records/history/jobs 表，并调用 `retrieval.initialize_indexes(conn)` 创建检索专用表。模块不得在 import 时创建库。

`jobs.JobStore(db)`：`enqueue(kind, resource_id, payload=None, not_before=None, bypass_window=False, *, conn=None, predecessor_id=None)`；`claim(owner)`；`renew(id,owner,token)`；`assert_lease(conn,id,owner,token)`；`finish(id,owner,token,result=None, mutate=None)`（mutate(conn) 与结果提交同事务）；`fail(...)`；`defer(...,until,reason)`；`cancel(id)`；`reschedule(id,not_before,bypass_window=False)`；`get(id)`；`list(limit=100, *, conn=None, include_active=False)`。时间为 Unix UTC 秒；claim 结果含 id,kind,resource_id,payload,checkpoint,lease_token,owner,status。checkpoint 通过 `checkpoint(id,owner,token,data,mutate=None)` 原子更新。

`enqueue`、`get`、`list` 和 `cancel` 可接受已有事务 `conn`，业务变更与任务创建／取消可以原子提交。`wait_for_review` 保存人工等待状态；`wake_book(book_id, conn=None)` 优先复用同书已有活动任务或最早等待任务；`resume_waiting(kind,resource_id,conn=None)` 在增加预算后恢复等待任务并保留检查点。依赖任务失败或取消时，依赖方明确失败；大量等待依赖的任务不能遮挡后续可执行任务。

worker 公共入口 `async run_worker(settings)`。CLI 提供 doctor/init/upgrade/password/web/worker/run。

worker 持有在途协程的强引用，完成回调释放引用，退出时取消并等待剩余任务。不限制在途任务数量；`book_process` 通过 `asyncio.gather` 并行识别草稿并按原输入顺序收集结果，不再设置单书页数信号量。所有模型请求仍经 `CapacityLimiter` 原子检查模型和凭据上限，正式跨页修订仍顺序执行。`max_active_jobs` 已从 `Settings` 移除。

`ACTIVE_STATUSES` 定义排队、运行、窗口等待、人工等待。`active_for`／`enqueue`／`reschedule` 在同一写事务内实现活动任务互斥：同类复用并返回 `reused=true`，教材处理／索引或题目识别／讲解的跨类型冲突抛出 `ConflictError`。`predecessor_id` 仅允许正在完成的同教材 `book_process` 原子创建 `book_index`。`request(kind,specification,create,*,conn=None)` 按序列化请求哈希查重，资源工厂 `create(conn)` 与任务写入同事务，用于搜索和导出；导出的题目版本读取也在同一事务内。`configuration_changed(conn)` 唤醒 `waiting_window` 的时段复核，不更改 `queued` 预约时间，与模型写入／删除共用事务。

## 模块边界

- ai.py/scheduling.py/worker.py/pipelines.py 提供 ModelProfile、模型调用、限流和任务处理器。任务类型包括 `model_test`、`question_extract`、`question_explain`、`book_process`、`page_recognize`、`book_index`、`suggestion_regenerate`、`search`、`export_pdf`；导出任务调用 export 模块。
- progress.py 提供 `JobProgress(db,conn).present(job)` 和 `present_jobs(db)`，将内部任务转换为页面可用的精简进度投影；不泄露检查点里的完整协议项、图片或凭据。
- lexical.py/textbook.py/context.py/retrieval.py 通过 records 存 book/page/node/block/concept/relation/suggestion/redirect 等；专用 FTS/vectors SQL 表由 retrieval.initialize_indexes(conn) 管理。
- frontend/ 使用 /api 同源接口；生产初始化不写演示数据。独立验收脚本只向隔离目录写入测试样本。

## 媒体

`media.store_upload(settings, filename, content:bytes) -> dict` 返回 asset {id,name,mime,path,url,preview_url,width,height}，元数据由 API 存 records kind=asset。`media.image_data_url(settings,asset:dict)->str`；`media.prepare_book_pages(settings,asset:dict)->list[dict]`（同步，只在 to_thread/子进程调，PDF内部用ProcessPool），结果 page fields source_asset_id,page_index,text,image_asset,image_path,source_type；`media.render_pdf_page(settings,asset,index,scale=2)->dict`；`media.crop_asset(settings,asset,box)->dict`。

## Web API / frontend 契约

工作台使用 hash 导航，未指定页面时进入 `#home`。首页复用科目、教材、题目、模型和任务的现有列表 API；`#questions?new=1` 与 `#books?new=1` 打开录入／导入窗口，`#questions?question=<id>` 与 `#books?book=<id>` 打开相应现有记录。深链接在资料加载后解析，目标不存在时显示提示；导航保留明确的页面选中状态。

返回实体直接 JSON；列表返回数组。错误 {detail:string|object}。登录外修改请求必须 X-CSRF-Token，GET /api/session 返回 {authenticated,csrf_token,initialized}；POST /api/login {password}；POST /api/logout。首次密码由 CLI init 交互设置，不开放浏览器无认证初始化。

GET/POST /api/subjects；GET/POST /api/questions；GET/PUT/DELETE /api/questions/{id}。Question fields id,revision,subject_id,type(single_choice|multiple_choice|fill_blank|short_answer),stem,options([{id,text}]),answer(any),answer_confirmed(bool),wrong_answer,notes,reference_text,asset_ids,reference_asset_ids,figure_asset_ids,book_ids,status,explanation(optional object),explanation_stale(bool)。PUT 携带 revision。POST /api/questions/{id}/confirm；POST /api/questions/{id}/extract 与 /explain body {not_before?:number,delay_seconds?:number,bypass_window?:bool} 返回 job。

Question 另有用户自填 `error_reason:string=""` 与 `optimize_error_reason:boolean=false`。旧记录在读取时补默认值，无需数据库架构迁移。优化结果为 `explanation.error_reason_optimized:string|null`；仅在已勾选、有原文且讲解未过期时展示，不覆盖原文。

GET/POST /api/books；GET/PUT/DELETE /api/books/{id}，fields title,subject_id,text,asset_ids,revision,status；POST /api/books/{id}/process 同 schedule。GET /api/books/{id}/pages,/nodes,/blocks,/suggestions；PUT /api/books/{id}/pages/{page_id} 手工草稿 {text,revision}；POST /api/books/{id}/pages/{page_id}/recognize,/skip,/text-only；POST /api/books/{id}/operations 提交操作组；POST /api/books/{id}/suggestions/{id}/accept,/ignore,/regenerate；GET /api/books/{id}/blocks/{id}/history。

教材的 `extra_processing_budget` 可为空；有限预算耗尽后增加预算会恢复索引任务。页面 `index` 是整本输入顺序，`page_index` 是原 PDF 内部页号。删除教材会在同一事务取消教材及页面／建议的相关任务，worker 提交时仍复核父教材状态。

GET/POST /api/models；PUT/DELETE /api/models/{id}；POST /api/models/{id}/test。Model fields name,role(vision|chat|embedding),protocol(chat|responses),base_url,api_key(读不返回),model,thinking(omit|enabled|disabled),reasoning_effort,temperature,top_p,max_output_tokens,max_tokens_field(max_completion_tokens|max_tokens),context_tokens,timeout_seconds,retries,max_tool_rounds,max_concurrency(default4),credential_max_concurrency(null),store(false),strict_tools(bool),extra_body(object),organization,project,auth_scope,windows([{start:"HH:MM",end:"HH:MM"}]),timezone,embedding_dimensions,embedding_revision,document_prefix,query_prefix。读返回 has_api_key、effective_max_concurrency。

POST /api/assets multipart file；GET /api/assets/{id}/file 与 /preview；POST /api/assets/{id}/crop {box:[x1,y1,x2,y2]}（归正图像素坐标）。POST /api/search {query,subject_id?,book_ids?,node_id?,mode:hybrid|keyword|phrase|semantic,keyword_mode:any|all,limit?}。

Model 另有 `tool_choice:required|auto|omit`，默认 `required`；`omit` 完全不发送工具选择参数。该字段同时适用于 Chat Completions 与 Responses，仍发送工具定义并校验结构化结果。旧模型记录由服务端补默认值，无需架构迁移。

Model 的 `max_output_tokens:int|null` 默认 `null`；前端新增配置默认为空，清空后提交 `null`。服务端拒绝 0／负数，缺省或 `null` 时完全省略请求中的 `max_completion_tokens`、`max_tokens` 和 `max_output_tokens`，不会发送值为 `null` 的字段；正整数按协议及 `max_tokens_field` 映射。已保存的显式上限保留，旧记录缺少此字段时按 `null` 处理，无需架构迁移。教材上下文未配置输出上限时仅以 `Settings.output_tokens` 预留内部预算，不将该预留值作为模型请求参数。

Model 的 `context_tokens:int|null` 默认 `null`；前端非必填，新增时留空，清空后提交 `null`，填写时最小为 1024。该字段仅用于应用内部输入预算，不映射到 Chat Completions、Responses 或 Embeddings 请求，也禁止通过 `extra_body` 发送。统一使用 `ModelProfile.effective_context_tokens(application_budget:int)->int`：缺省／`null` 返回应用预算，显式整数返回它与应用预算的较小值。调用方使用 `Settings.context_tokens`（默认 24000）作为应用预算；工具请求、教材修订、概述分段／合批及嵌入分批均复用这一规则。已有显式值保留，旧记录缺少字段时按 `null` 处理，无需架构迁移；清空不取消教材长上下文保护。

无需嵌入请求的检索同步返回结果数组；配置了嵌入模型的 semantic/hybrid 查询返回 HTTP 202 `{job: ...}`，由 worker 获取查询向量。前端从任务结果 `result.hits` 读取命中，不绕过 worker 直接调用服务商。校对操作组必须提供应用生成的 `operation_group_id`（也接受 `group_id`）；没有 ID 明确报错。

GET /api/jobs；POST /api/jobs/{id}/cancel,/retry,/reschedule。POST /api/exports {question_ids,mode:practice|review,include_answer,include_explanation,include_knowledge,blank_lines} 返回job；GET /api/exports/{id} 返回 snapshot/status/url；GET /api/exports/{id}/file 下载。GET /api/export-snapshot/{id}?token=... 供独立导出路由，token限快照；前端 /print/{id}?token=... 使用该接口共用内容渲染，设置 window.__STUDYQUIP_PRINT_READY__ / __STUDYQUIP_PRINT_ERROR__。

所有返回任务的 Web API 统一使用投影：保留 `id/kind/resource_id/status/created_at/updated_at/not_before/attempts/error/result/bypass_window/reused`，增加 `resource_title/book_id/question_ids/blocking/recovering/waiting_reason/progress`；仅搜索任务提供恢复表单所需的 `input`。不再返回内部 `checkpoint/payload/owner/lease_token`。`GET /api/jobs` 包含最近 100 条及更早的全部活动任务。

`progress` 包含可选的 `phase/current_page/total_pages/recognized_pages/processed_pages/review_pages/skipped_pages/pending_pages/blocks/nodes/summarized_nodes/embedding_total/embedding_completed`，以及 `completed_units/completed_stages/usage/active_requests/embedding_activity/last_activity_at`。请求活动字段为 `state/at/attempt/model/revision/role/page`（按来源可缺省）。页数来自当前正式数据；原页内部顺序不进入对外教材引用。用量为已保存响应累计，原始检查点仅供 worker 使用。

前端 `JobsProvider` 在工作台路由外共享状态，写操作通过 `studyquip:jobs-changed` 通知立即合并任务并刷新；轮询间隔集中为 3000 ms。处理按钮在加载／错误／已有活动任务时禁用；任务结束通知对应页面刷新已提交结果，原页编辑保留未保存文本与基础版本。检索从 `input` 和 `result.hits` 恢复。模型记录携带 `revision`，热重载在处理单元／请求边界实施，协议见 ai-runtime.md。
