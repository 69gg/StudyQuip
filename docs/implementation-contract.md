# 模块与接口约定

本文件记录模块之间的实际接口；运行方式、教材协议与模型协议分别见 README、operations.md、textbook-protocol.md 和 ai-runtime.md。

## 后端公共基础

包：`studyquip`，源码位于 `src/studyquip`。所有 Python 函数添加类型注释。

`config.Settings` 为 Pydantic Settings：data_dir(Path)、host、port、allowed_origins(list[str])、lease_seconds=90、heartbeat_seconds=15、busy_timeout_ms=5000、output_tokens=4096、max_upload_mb=1024、worker_poll_seconds=1、frontend_dir(Path)。属性 db_path、files_dir。`Settings()` 读 STUDYQUIP_ 环境变量。上传限制按 MiB 换算，默认 1 GiB；API 的读取边界与媒体层校验共用该值，等于上限允许上传，超过拒绝，运行中实例不热重载此应用级配置。应用级 `context_tokens` 已删除，旧 `STUDYQUIP_CONTEXT_TOKENS` 被忽略；上下文仅由各模型的可空配置决定。

`db.Database(settings)` 持有 rw/ro SQLAlchemy engine。`write()` 是返回 Connection 的短事务 context manager；`read()` 是只读 Connection context manager。`get(kind, id, conn=None)` 返回 dict 或 None；`list(kind, *, filters=None, limit=1000, offset=0, conn=None)` 返回 dict 列表，filters 对 data JSON 顶层字段精确比较。`put(kind, data, *, id=None, expected_revision=None, conn=None)` 返回带 id/revision/created_at/updated_at 的 dict；存在时未提供 expected_revision 视为调用方已在同一写事务锁定，外部修改必须明确传版本。`delete(kind,id,*,conn=None)`。`history(kind,id,conn=None)` 返回旧/现版本列表。`secret()` 返回持久化 bytes。`ConflictError` 表示版本冲突。records 表为(kind,id,revision,data JSON,created_at,updated_at)，history 保存每次版本。

数据库初始化集中在 `db.initialize(settings)`，先 doctor 再独占迁移锁，Alembic 创建公共 records/history/jobs 表，并调用 `retrieval.initialize_indexes(conn)` 创建检索专用表。模块不得在 import 时创建库。

`jobs.JobStore(db)`：`enqueue(kind, resource_id, payload=None, not_before=None, bypass_window=False, *, conn=None, predecessor_id=None)`；`claim(owner)`；`renew(id,owner,token)`；`assert_lease(conn,id,owner,token)`；`finish(id,owner,token,result=None, mutate=None)`（mutate(conn) 与结果提交同事务）；`fail(...)`；`defer(...,until,reason)`；`cancel(id)`；`reschedule(id,not_before,bypass_window=False)`；`get(id)`；`list(limit=100, *, conn=None, include_active=False)`。时间为 Unix UTC 秒；claim 结果含 id,kind,resource_id,payload,checkpoint,lease_token,owner,status。checkpoint 通过 `checkpoint(id,owner,token,data,mutate=None)` 原子更新。

`enqueue`、`get`、`list` 和 `cancel` 可接受已有事务 `conn`，业务变更与任务创建／取消可以原子提交。`wait_for_review` 保存人工等待状态；`wake_book(book_id, conn=None)` 优先复用同书已有活动任务或最早等待任务；`resume_waiting(kind,resource_id,conn=None)` 在增加预算后恢复等待任务并保留检查点。依赖任务失败或取消时，依赖方明确失败；大量等待依赖的任务不能遮挡后续可执行任务。

`resume(id,not_before,bypass_window=False)` 复用原任务和检查点，接受失败／取消／待人工处理状态；对于已在队列、运行或等待时段的同一任务幂等返回，不改变预约和租约。`POST /api/jobs/{id}/resume` 与兼容 `/retry` 接收可选 `ScheduleInput`，修复旧重试接口忽略预约请求体的行为。`resume_problem` 与执行层共用来源指纹，恢复前拒绝互斥任务、已删除来源及输入修改，已完成任务不能恢复。无需数据库迁移。

`ModelProfile.retries` 同时作为网络重试次数和每个结构化单元的格式重试次数，分别计数，默认 2、0 表示关闭。`AIService.structured` 在没有工具调用、空 choices、JSON／字段校验错误时保存反馈后自动重试，通过现有 `_request` 执行准入；`scheduling.retry_delay(attempt)` 共用指数退避。阶段新增 `format_errors`（当前回复尚待处理的校验错误）、`format_retries`（已安排次数）、`format_rounds`（格式失败回复数）、`format_retry:{attempt,limit,next_at,reason}` 和耗尽标志 `format_failure`；`rounds-format_rounds` 受正常工具轮数约束。`pending_batch_size` 保留原工具批次大小。显式恢复已结束任务只清除未完成且耗尽阶段的 `format_failure/format_retries/format_retry`，保留转录、用量和已完成结果；自动恢复不清额度。旧 `repairs` 不再触发固定一次修复限制。

`RevisionDraft.operations` 通过 `op` 判别联合类型；`operation_schema` 保留发往服务商的 `anyOf` 结构。AI 结果数组共用 `ModelList[T]`／`decode_model_list`：仅解码单键 `item` 包装和标准 JSON 数组字符串，再验证全部元素；其他类型错误不猜测修复。无名 `Annotated` 类型别名保持原有 JSON Schema，不修改普通字符串或 HTTP 业务输入。`validation_feedback` 输出字段路径、错误信息与类型，省略原始参数和错误文档链接。活动投影另有 `format_attempt/format_limit/reason`，供现有任务界面展示。

独立结果提交在校验前保存 `rejected_result:{call_id,name,arguments}`，错误反馈和候选按既有检查点提交；更新回复到达后清除旧候选。`rejected_result(state,protocol)` 拒绝未完成批次，兼容从旧 Chat 末尾的独立提交／匹配错误反馈重建候选。显式继续且模型配置兼容时，先以当前规则解析最后候选，即使工具定义已更新也允许恢复；成功记录 `result_recovered_from_call_id/result_recovered_on_schema_change` 并进入既有业务提交，保留原转录与用量。配置变化或候选无效且工具结构变化时才清除未完成协议并重建，不以旧结果替代新模型运行。原页版本、输入指纹、操作回执和租约仍逐层校验。

`AIService.structured` 用 `model_validate(..., context={"rejections": []})` 接受结果。只有 `RevisionDraft` 的可选概念／关系使用该审计收集器逐条验证；必需操作和其他任务的 schema 仍严格。错误项写入阶段 `validation_rejections`；正文提交事务同步落 `relation_rejection` 并累计检查点 `enrichment:{concepts,relations,rejected}`。格式正确的增强数据仍按现有证据规则验证。新工具定义说明正文优先及可省略增强，升级时无完整可恢复候选的旧工具链会重建当前单元，已完成单元不重做。

读取工具的参数或查无结果错误只计 `tool_errors` 和正常工具轮数，不消耗 `format_retries`。`tool_events` 记录最近工具名、轮数、耗时、状态与安全错误消息，按 `Settings.task_activity_history_size=12` 保存；此值不限制协议转录、执行轮数或并发。任务投影增加 `enrichment/tool_errors/tool_events`；请求活动增加 `tool_name/stream_phase/retry_limit/last_failure`。504 和本地等待超时分别报告，后续重试保留上次失败耗时。正文单元数与 OCR 阶段数分别展示。

Chat 流式累积将 `role` 作为枚举元数据处理，重复／延后／省略 `assistant` 不会生成重复角色字符串。显式非助手角色触发 `StreamProtocolError`，请求活动失败状态为 `protocol`，不作网络重试；完整普通回复也拒绝非助手角色。`repair_chat_roles(transcript)->int` 在恢复链执行待处理工具前校验全部消息角色，只修复完整重复的 `assistant`，返回修复条数并累加阶段 `chat_role_repairs`，随已有检查点保存。其他协议字段、已完成工具、剩余 `pending` 和用量保留；没有批量修改数据库或新增迁移。

模型阶段新增 `request_context:{system,prompt,image_hashes,tools}` 保存初始输入，完整转录与逐个工具结果继续保存在同一阶段。恢复时先读最新模型，配置指纹或工具定义变化则重建未完成工具链并清除此输入快照，保留累计用量与已完成结果。兼容旧检查点：缺少该字段时只补建初始输入，配置相同的 `transcript/pending/rounds/usage` 仍复用。图片按指纹验证，Base64 不在检查点重复保存。教材 `revision_plans[page_id:revision]` 仅继续使用 `unit_budget` 固定已划分的边界（`null` 表示整页）；旧 `context_budget` 不再约束恢复，总预算使用最新模型值。

`ai.ModelRole` 为 `book_vision|book_text|question_vision|question_text|embedding|speech_recognition|speech_synthesis`。`AIService.profile_for(role=None,explicit_id=None)` 必须指定用途或 ID；连接测试使用 ID，其余生成按用途读取。教材草稿用 `book_vision`，修订、概述和建议用 `book_text`；题目有题图或参考图时用 `question_vision`，纯文字提取及讲解用 `question_text`；索引及查询向量共用 `embedding`。当前 API 新增配置默认 `question_text`，不再接受旧用途作为新配置。

`ai.split_legacy_model_roles(db)->int` 由 Web lifespan 和 `Worker.run()` 在服务请求／认领任务前调用，返回转换的旧记录数。旧 `vision` 原 ID 改为 `book_vision` 并复制 `question_vision`；旧 `chat` 原 ID 改为 `book_text` 并复制 `question_text`。复制 ID 使用 `uuid5(NAMESPACE_URL,"studyquip:model:{source_id}:{role}")`；字段完整继承，嵌入不变。同一短写事务重新检测旧用途并提交全部改动，不引入新表或付费请求。已转换记录后续不会再次填充，已有复制记录不覆盖。绑定 HMAC 将新的图片／文本用途映射回旧分类保持兼容，只有拆分用途时不清掉未完成工具链。凭据与模型桶的身份定义保持独立于用途。

worker 公共入口 `async run_worker(settings)`。CLI 提供 doctor/init/upgrade/password/web/worker/run。

`Worker.run()` 在认领前调用 `JobStore.restore_review_pages()->int`，逐页短事务恢复未删除教材的 `needs_review` 页面。`TextbookService.restore_review_page(book_id,page_id,conn)` 保留人工编辑，否则采用已有 AI 候选，无候选保留当前文本；转为 `draft` 并移除旧质量字段，历史仍递增。只有旧页面质量等待被重新排队，不改变预约、检查点或其他终态。识别入口复用同一转换；新 `PageDraft{text:string,is_blank:boolean=false}` 兼容忽略旧 `quality/issues`，识别成功即存草稿，无质量补救请求。

worker 持有在途协程的强引用，完成回调释放引用，退出时取消并等待剩余任务。不限制在途任务数量；`book_process` 通过 `asyncio.gather` 并行识别草稿并按原输入顺序收集结果，不再设置单书页数信号量。所有模型请求仍经 `CapacityLimiter` 原子检查模型和凭据上限，正式跨页修订仍顺序执行。`max_active_jobs` 已从 `Settings` 移除。

`ACTIVE_STATUSES` 定义排队、运行、窗口等待、人工等待。`active_for`／`enqueue`／`reschedule` 在同一写事务内实现活动任务互斥：同类复用并返回 `reused=true`，教材处理／索引或题目识别／讲解的跨类型冲突抛出 `ConflictError`。`predecessor_id` 仅允许正在完成的同教材 `book_process` 原子创建 `book_index`。`request(kind,specification,create,*,conn=None)` 按序列化请求哈希查重，资源工厂 `create(conn)` 与任务写入同事务，用于搜索和导出；导出的题目版本读取也在同一事务内。`configuration_changed(conn)` 唤醒 `waiting_window` 的时段复核，不更改 `queued` 预约时间，与模型写入／删除共用事务。

## 模块边界

- ai.py/scheduling.py/worker.py/pipelines.py 提供 ModelProfile、模型调用、限流和任务处理器。任务类型包括 `model_test`、`question_extract`、`question_explain`、`question_audio`、`question_index`、`book_process`、`page_recognize`、`book_index`、`suggestion_regenerate`、`search`、`export_pdf`；导出任务调用 export 模块。
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

“AI 整理题目”在 `QuestionDraft.stem/options` 中返回规范公式排版后的完整文本，并返回 `formatting_issues:[{field:stem|option,option_id?:string|null,message:string}]`，无问题为 `[]`；缺省／`null` 兼容旧工具结果，仅补空字段。`question_text_fields(question,draft)` 按现有选项 ID 合并文本，保留顺序，拒绝不完整或重复的候选 ID；有问题的已有字段保留原文，未填写答案且全空的选项占位列表可以重建。应用只读输出 `formatting_warnings?:string[]` 用于核对提示，手动修改题干或选项后清除；内容变化或出现提示后要求重新确认答案。原答案值保持不变，旧讲解过期，历史由既有 `record_history` 保留。无需结构迁移，也不增加独立格式整理任务。

GET/POST /api/books；GET/PUT/DELETE /api/books/{id}，fields title,subject_id,text,asset_ids,revision,status；POST /api/books/{id}/process 同 schedule。GET /api/books/{id}/pages,/nodes,/blocks,/suggestions；PUT /api/books/{id}/pages/{page_id} 手工草稿 {text,revision}；POST /api/books/{id}/pages/{page_id}/recognize,/skip,/text-only；POST /api/books/{id}/operations 提交操作组；POST /api/books/{id}/suggestions/{id}/accept,/ignore,/regenerate；GET /api/books/{id}/blocks/{id}/history。

教材的 `extra_processing_budget` 可为空；有限预算耗尽后增加预算会恢复索引任务。页面 `index` 是整本输入顺序，`page_index` 是原 PDF 内部页号。删除教材会在同一事务取消教材及页面／建议的相关任务，worker 提交时仍复核父教材状态。

GET/POST /api/models；PUT/DELETE /api/models/{id}；POST /api/models/{id}/test。Model fields name,role(book_vision|book_text|question_vision|question_text|embedding),protocol(chat|responses),base_url,api_key(读不返回),model,thinking(omit|enabled|disabled),reasoning_effort,temperature,top_p,max_output_tokens,max_tokens_field(max_completion_tokens|max_tokens),context_tokens,timeout_seconds,retries,max_tool_rounds,max_concurrency(default4),credential_max_concurrency(null),store(false),strict_tools(bool),extra_body(object),organization,project,auth_scope,windows([{start:"HH:MM",end:"HH:MM"}]),timezone,embedding_dimensions,embedding_revision,document_prefix,query_prefix。读返回 has_api_key、effective_max_concurrency。

POST /api/assets multipart file；GET /api/assets/{id}/file 与 /preview；POST /api/assets/{id}/crop {box:[x1,y1,x2,y2]}（归正图像素坐标）。POST /api/search {query,subject_id?,book_ids?,node_id?,target:book|question,methods:[keyword|semantic],keyword_mode:any|all|phrase,parts?,question_types?,confirmed_only?,limit?,context:search|print}。

Model 另有 `tool_choice:required|auto|omit`，默认 `required`；`omit` 完全不发送工具选择参数。该字段同时适用于 Chat Completions 与 Responses，仍发送工具定义并校验结构化结果。旧模型记录由服务端补默认值，无需架构迁移。

Model 的 `max_output_tokens:int|null` 默认 `null`；前端新增配置默认为空，清空后提交 `null`。服务端拒绝 0／负数，缺省或 `null` 时完全省略请求中的 `max_completion_tokens`、`max_tokens` 和 `max_output_tokens`，不会发送值为 `null` 的字段；正整数按协议及 `max_tokens_field` 映射。已保存的显式上限保留，旧记录缺少此字段时按 `null` 处理，无需架构迁移。仅在显式填写上下文预算而最大输出留空时，以 `Settings.output_tokens` 预留内部预算，不将预留值作为模型请求参数。

Model 的 `context_tokens:int|null` 默认 `null`；前端非必填，新增时留空，清空后提交 `null`，填写时最小为 1024。该字段不映射到 Chat Completions、Responses 或 Embeddings 请求，也禁止通过 `extra_body` 发送。为空时省略完整请求、材料组装、正文读取及概述／嵌入分批的长度限制；删除 `effective_context_tokens` 的应用预算回退。显式上限才触发材料分段和完整请求检查，报错包含保守估算及设置值；已有显式值保留，无需架构迁移。`ContextBuilder(db,token_budget:int|None=None)` 支持整页无长度限制；`retrieval_tools(...,context_tokens:int|None=None)` 以同一配置限制读取，仍始终验证教材范围及来源版本。完整协议项不被裁剪。

无需嵌入请求的检索同步返回结果数组；包含 semantic 步骤的查询返回 HTTP 202 `{job: ...}`，由 worker 获取查询向量。前端从任务结果 `result.hits` 读取命中，不绕过 worker 直接调用服务商。校对操作组必须提供应用生成的 `operation_group_id`（也接受 `group_id`）；没有 ID 明确报错。

GET /api/jobs；POST /api/jobs/{id}/cancel,/retry,/reschedule。POST /api/exports {question_ids,mode:practice|review,include_answer,include_explanation,include_knowledge,blank_lines} 返回job；GET /api/exports/{id} 返回 snapshot/status/url；GET /api/exports/{id}/file 下载。GET /api/export-snapshot/{id}?token=... 供独立导出路由，token限快照；前端 /print/{id}?token=... 使用该接口共用内容渲染，设置 window.__STUDYQUIP_PRINT_READY__ / __STUDYQUIP_PRINT_ERROR__。

所有返回任务的 Web API 统一使用投影：保留 `id/kind/resource_id/status/created_at/updated_at/not_before/attempts/error/result/bypass_window/reused`，增加 `resource_title/book_id/question_ids/blocking/recovering/waiting_reason/progress`；仅搜索任务提供恢复表单所需的 `input`。不再返回内部 `checkpoint/payload/owner/lease_token`。`GET /api/jobs` 包含最近 100 条及更早的全部活动任务。

`progress` 包含可选的 `phase/current_page/total_pages/recognized_pages/processed_pages/review_pages/skipped_pages/pending_pages/blocks/nodes/summarized_nodes/embedding_total/embedding_completed`，以及 `completed_units/completed_stages/usage/active_requests/embedding_activity/last_activity_at`。请求活动字段为 `state/at/attempt/model/revision/role/page`（按来源可缺省）。页数来自当前正式数据；原页内部顺序不进入对外教材引用。用量为已保存响应累计，原始检查点仅供 worker 使用。

同一投影的 `resume:{available,has_saved_progress,reason}` 驱动续接入口和不可续接提示；`request_context` 与完整工具转录不得进入此响应。前端共用 `ResumeJobButton` 与原预约弹窗，提交后利用已有任务事件同步状态。

前端 `JobsProvider` 在工作台路由外共享状态，写操作通过 `studyquip:jobs-changed` 通知立即合并任务并刷新；轮询间隔集中为 3000 ms。处理按钮在加载／错误／已有活动任务时禁用；任务结束通知对应页面刷新已提交结果，原页编辑保留未保存文本与基础版本。检索从 `input` 和 `result.hits` 恢复。模型记录携带 `revision`，热重载在处理单元／请求边界实施，协议见 ai-runtime.md。

前端 `roleLabels/roleDescriptions` 定义五种用途及职责，模型列表、用途选择和字段说明复用；首页的题目录入引导检查 `question_vision/question_text`，仅配置教材用途不视为题目模型已就绪。


## 递归题目与科目扩展

题目保持单个 `records(kind=question)` JSON 和根 revision，不新增业务表；`parts` 递归包含同结构节点，type 为 composite 或现有四种基础题型，不配置固定嵌套层数。大题可无题干，必须至少一个小题；叶题仍执行原字段与答案约束。root subject_id/book_ids 约束整题，root answer_confirmed 表示全部答案经用户确认。节点／材料 ID 在整题唯一。`QuestionInput` 只接受可编辑字段，更新按子题 ID 合并并保留服务端解析；比较规范化编辑投影避免一次普通保存误丢结果或撤销答案确认。只改备注／错因不撤销答案确认，相关内容变更使解析过期。

`materials` 包括 id、kind(text|listening)、可空 title、text、audio_asset_id、audio_generated_from；听力在确认时至少文稿／音频一项。`POST /api/questions/{id}/audio` 接受 ScheduleInput，生成 question_audio 持久任务。题目 API 投影 `audio_pending_roles`，Web 保存后有相应模型时自动安排缺失项；手动补全可预约，两个都已提供时不调用。材料音频引用、所有层级图片引用必须指向已有相应 MIME 的附件。音频支持 WAV、MP3、M4A/MP4、Ogg/Opus、FLAC、WebM 容器头，浏览器播放能力依格式而定。

`subjects.ensure_default_subjects` 在 Web／worker 启动时执行短事务。默认列表配置 `default_subjects`；NFC＋strip 同名复用 ID，以 app_state 中的预设名称→ID 标记避免重命名后重建旧名。`POST /api/subjects` 同名返回原科目；`PUT /api/subjects/{id}` 带 name/revision，保持 ID，撞名拒绝而不合并题目或教材。

导出递归检查所有基础题答案与讲解有效性，快照保留完整树。签名附件权限递归包含子题配图；当前 API 投影与历史快照分开，不改写旧引用。Print/QuestionContent 共享多级题号、材料、SVG/plot 与上传插图，练习隐藏答案、解析、听力文稿，仅简答题按 blank_lines 留白；复习展示选择的解析、错因和教材路径。等待字体、图片解码、KaTeX 和图形渲染错误检测后才生成 PDF。


## 题目分部索引与查询契约（0002）

`question_index.QuestionIndex.sync(question,conn)` 在 `Database.put("question",...)` 的同一事务更新投影、清除变更部分的旧向量，并按需去重入队 `question_index`。`Database.delete` 同样清理派生表。`reconcile_question_indexes(db)` 用于启动补齐和嵌入空间变化，每题单独事务，不生成题目历史版本；缺少模型时只保留关键词索引。模型请求全部使用已有 worker。

`question_parts` 以 uuid5(root_question_id,node_id,part) 生成稳定 ID，保存 question_id/node_id/part/node_type/path/question_revision/text/content_fp/lexical_fp。`part` 为 stem/options/answer/explanation/knowledge/material/reference/error_reason；路径按实际嵌套序号生成，无固定深度。`content_fp` 由有版本提取规则与 NFC 完整文字生成，根题的 revision 与文字指纹分离。`question_fts` 使用共享 jieba/词法规范。`question_vectors` 以 part_id/space_fp 为主键，带 content_fp、dim、dtype、BLOB，删除部分通过外键级联移除向量。修改即时失效，未改文字跨题目版本复用；不改题目本身来保存索引状态。

`QuestionIndex.search` 在同一个只读快照校验当前题目版本、删除状态、解析引文版本，再执行 FTS 或预筛选的物化向量计算；同表多维度先按空间／维度／长度过滤，使用受保护距离表达式。一个根题只返回所选部分中最匹配的一处，附 question_id/question_revision/node_id/path/part/part_label/text/method/rank/score；题型条件可匹配根题或命中子题的自身类型。部分之间不拼接短语。

`SearchInput.methods` 默认仅 `["keyword"]`，长度 1–2，禁止重复；顺序有效。单方式兼容字段 `mode=keyword|semantic|phrase` 仍接受，phrase 转为关键词短语，旧组合值明确拒绝。`limit` 留空使用集中配置，超出返回 422；LLM 工具使用更小上限。每步独立读取当前内容，最终再次排除前面步骤中已过期的命中，按 step/rank 顺序追加、以根题／教材块 ID 去重，不混合分数，不对前一步结果再次筛选。两个方式显式开启后每种最多 limit，总数可能为 2×limit；默认只执行一个。`GET /api/search/options` 返回部分标签、默认方式、默认条数及最大条数，UI 复用服务端定义。题目 target 不接受教材目录条件。

题目保存立即产生待处理索引，默认延迟 2 秒合并短间隔编辑，模型窗口和并发上限保持原规则。同一题只存在一个活动索引任务，与 question_extract/question_explain/question_audio 不互斥。每批提交校验租约、部分内容指纹与最新空间；结束前同事务复核 pending，出现新内容转为等待下一次执行，不能漏掉最终编辑。已失败索引可以手动继续，启动或空间变化也会为仍缺失的部分重新排队。其他题目处理任务的源版本保护保持不变。
