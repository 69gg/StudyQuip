# 教材修订与检索协议

本协议对应 `textbook.py`、`lexical.py`、`context.py` 和 `retrieval.py`。源文件、页草稿与正式内容分离；只有正式内容执行 NFC。坐标使用正式版本的 Unicode 码点区间 `[start,end)`，不能直接使用 JavaScript UTF-16 索引。

## 正式内容与目录

目录是任意深度树，节点可以同时拥有正文和子节点。节点字段为 `id/book_id/parent_id/title/order/revision`；每本教材有稳定根节点。正文块使用 `id/book_id/node_id/text/type/order/source_page_ids/asset_ids/revision/human_protected/archived`。显示引用时使用书名与实际目录路径，页序只用于内部校对。

所有变更经过 `TextbookService.apply_operations`。参数 `group_id` 标识一次提交，`operations` 支持：

- `insert`：`id` 可由调用方提供；`block` 包含 `text`、可选 `node_id/type/order/source_page_ids/asset_ids`。
- `update`：`id/base_revision/changes`；支持 `text_edit:{start,end,expected_text,replacement}` 的精确局部修改。
- `move`：`id/base_revision/node_id/order`。
- `split`：`id/base_revision/parts`，其中 `parts` 是完整分割后文本数组。
- `merge`：`ids/base_revisions`，可选合并后 `text`，缺省按原順序以空行连接。
- `archive`：`id/base_revision`。
- `node`：`node:{id?,parent_id,title,order?,base_revision?}`。

已有对象必须提供基础版本。整个操作组先在内存模拟，再在 SQLite savepoint 内提交；任何业务拒绝回滚该组，拒绝回执保存于外层事务。worker 在同一外层事务中校验租约并提交检查点。数据库锁超时或崩溃导致外层事务回滚时，不留下假终态。

操作组成功或业务拒绝后 ID 即耗尽。相同 ID、相同载荷返回原回执；不同载荷返回 `idempotency_conflict`。修正版必须使用新 ID。回执与该教材共存，不随任务重试清除。

## 人工保护、重定向和证据

人工修改自动受保护。AI 触及受保护块或目录时形成建议，新的目录节点及正文插入部分先独立保存，再把修改已有内容、合并等剩余操作交给审阅。建议记录前后版本、理由和基础版本；采纳重新检查版本，过期转为 `stale`。忽略相同提案与基础版本后，不重复制造建议。

合并保留当前文档顺序最前 ID；当前重定向表扁平化到最终存活块，没有查询递归。拆分保留首块 ID。旧版本到新块的区间迁移独立记录。FTS 在同一事务重建对应当前块，旧向量失效。概念、关系和当前引用投影的证据必须重新定位并验证；不能验证则标记 `migration_unverifiable`。历史讲解快照不被重定向改写。

引文基础规范化固定为 NFC、连续 `str.isspace()` 空白折叠为一个普通空格、去除首尾空白。标点、全半角和 OCR 替字不自动等价，数学符号不被 NFKC 改写。库侧空白折叠保留回到正式文本的坐标映射。

引文必须带块 ID、版本、原句，重复出现时还需 `span_hint` 消歧。硬拒绝原因分别为：`empty_quote`、`normalized_quote_mismatch`、`ambiguous_quote`、`span_out_of_bounds`、`span_hint_mismatch`、`stale_source_revision`、`source_out_of_scope`。近似检索只能辅助找原文，不能成为证据校验的捷径。关系无证据或者达到数量上限时写入拒绝记录，不建立活动边。 非结构关系数量按当前有效关系统计：每块默认 3 条、每概念默认 6 条、每目录节点默认 12 条，均合并计算 source 与 target 两端，入边不能绕过上限；更新同一关系不重复占位。上限由 Settings 集中配置。

## 长书上下文

每页先形成草稿，正式修订顺序执行。`ContextBuilder.build` 装配书名、当前目录祖先、有限相关目录、当前页处理单元、前两页最新块、可选后一页草稿、工作记忆和建议句柄。此前页面跳过会形成缺口，不跨缺口拼句。

未知模型 tokenizer 时用 UTF-8 字节数作保守上界，工具定义计入预算。先丢弃整份可选后一页、无关目录和较远近邻原文，保留可按 ID 读取的句柄；不截断当前修改目标。超长页通过 `units()` 无损拆分，所有单元拼接严格恢复输入。`current_page.unit_index/unit_count` 让执行层在最后单元完成前不把整页标为完成。

远处块通过 `read_block` 获取最新版本，可指定码点范围。单次读取超过预算明确拒绝并要求缩小范围。摘要不能替代目标原文。`node_source_fingerprint` 基于直属内容版本、子节点概述来源和当前标题，修改后按受影响祖先标记过期。

## 混合检索与重建

FTS 统一使用 jieba 不启用 HMM 的搜索分词，包含标题、目录路径、正文、概念名称和别名。ANY 使用搜索词 OR；ALL 使用非重叠分词 AND；所有字面量转义，用户不能直接注入 FTS 运算符。短语采用规范化全文的连续匹配。空查询返回空。词法指纹包含 Unicode、jieba、词典内容、规范化版本和大小写规则，指纹变化要求重建。

向量存放普通 BLOB 表，空间指纹不含凭据，包含端点、模型修订、维度、前缀与语义参数。写入验证有限、非零 float32；查询先通过 MATERIALIZED 集合过滤空间、维度、书本、当前版本及可选子树，再通过 CASE 保护执行距离计算。

混合检索组合全文关键词、全文向量、目录标题／概述及目录向量路由、子树补充、概念关系和近邻块。范围在服务端校验；目录候选不能排除全局直接命中。RRF 仅是相对排序分数，不能在界面解释成准确率或置信百分比。返回 `book_title/path/score/channels`；`path` 不含教材页码。最终交给讲解模型的仍是当前有效正文。

数据库恢复或词法规则改变后，`RetrievalService.rebuild(book_id)` 重建该书 FTS；嵌入由 `embedding_targets/store_embedding` 重新生成。内容改动期间 FTS 保持可用，过期空间向量不会混入新查询。

## 历史恢复

`restore:{id,base_revision,restore_revision}` 读取本书对应块的历史版本，并创建新的正式版本。普通文本回退不会减小版本号或删除历史。若跨越合并／拆分，或者恢复目标已经重定向，单块操作拒绝并返回 `restore_requires_topology_group` 及关联 ID；必须在同一操作组列出所有拓扑关联块的 `restore`。`restore_revision:null` 表示归档后来拆分生成的块。恢复组只包含恢复操作，不与普通编辑混合。

整组恢复同时重建正式内容、当前重定向、FTS、失效向量和当前证据。已取消的重定向保存为 `current_id:null/active:false` 的当前墓碑，使再次合并仍可沿用递增历史版本；解析器将其视为没有重定向。旧快照与重定向历史不删除。引文回迁到恢复后的块必须唯一匹配；歧义或跨块证据标记失效。
