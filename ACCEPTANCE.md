# SkillScope acceptance

本文档区分三层验收：代码正确性、场景稳定性和产品闭环。正式 user study 前三层都必须通过。

## 0. 四条 participant 链路

每条链路都从右上角“重置”开始，确认页面回到工作流选择，再选择表中场景并阅读工单。上一条的记录会被清空，这是为了让四次走查互不污染。

| 链路 | 地址 | 选择的工作流 |
|---|---|---|
| W-T | `http://127.0.0.1:8775/` | 差旅中断改签 |
| W-E | `http://127.0.0.1:8775/` | 报销票据审核 |
| C-T | `http://127.0.0.1:8775/chat` | 差旅中断改签 |
| C-E | `http://127.0.0.1:8775/chat` | 报销票据审核 |

每条都走到发布或暂缓、完成行为预测与任务结束问卷。另做一次非破坏性的切换检查：在尚未重置时切到另一工作流，再切回，确认原进度恢复而不是生成重复记录。正式链接不要在共享 8775 进程上测试；按第 4 节的隔离启动命令验收。

## 1. 启动前检查

```bash
cd /root/workspace/paper/skillscope
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py scenario_runtime.py scripts/*.py
git diff --check
```

预期：全部 tests 通过，Python 编译与 diff 检查无输出、退出码为 0。

## 2. 黑盒工具执行

在一个临时端口启动服务：

```bash
DEEPSEEK_API_KEY=... PORT=8776 python3 server.py
python3 scripts/acceptance.py http://127.0.0.1:8776 travel-rebooking 5
python3 scripts/acceptance.py http://127.0.0.1:8776 expense-review 5
```

验收条件：

- 每个公开 run 都有 `execution.runtime=skillscope/tool-world/1`；
- `facts_source=tool-trace-and-world-state`；
- trace 中是 typed tool calls 及其真实参数/返回值，而非模型编写的伪步骤；
- 相同 case 的 `world_hash`、`tool_schema_hash`、`case_hash` 固定；每次运行使用隔离世界；
- HTTP response 不出现 `_oracle`，导出的 researcher record 保留 `_oracle`；
- temperature=0 的 smoke run 中，faulty incident 应稳定选择错误分支且 hidden oracle 不通过。

## 3. 完整闭环自动验收

对临时服务运行：

```bash
python3 scripts/e2e_acceptance.py http://127.0.0.1:8776 travel-rebooking
# 在另一个空白临时进程上：
python3 scripts/e2e_acceptance.py http://127.0.0.1:8777 expense-review
```

脚本会执行并断言：incident baseline → Change → 两个 Preserve 与一个 Unresolved 邻近情况 → immutable scope v5 → 自动单指令修改依据（delete + invert，各 3 次，带 provenance）→ exposure-aware manifest（3 visible / 1 withheld）→ AI draft → owner edit → 每侧 3 次 matched executions → Needs judgment → release record → 3 个 regression assets → export。预期输出以 `PASS:` 开头。

脚本会发布一个测试版本，不要对正式采集进程运行。单元测试中的 `test_all_four_counterbalanced_cells_freeze_the_assignment` 还会检查 travel/expense × workspace/chat 四个正式任务单元的场景、条件与工单 hash 均被冻结。

## 4. 人工产品验收

打开结构化条件 `/`，按以下路径检查：

1. 选择“差旅中断改签”。系统应先显示“评审工单”，包含业务角色、事件背景、目标、完成条件、建议用时和隔离环境说明；此时不得执行历史事件，也不得出现具体修复答案。点击“接受工单并开始评审”后，历史事故才自动运行 3 次。开始后右上角“评审工单”可随时重新打开，且不会重置状态。
2. AI 与工具仍在执行时，工作区只能显示执行进度，不得提前出现“需要处理的问题”。在工作流中确认 agent 依次读取日历、搜索航班并完成改签；原 skill 应稳定选择便宜但迟到的 `UA1123`。
3. 三次执行全部完成后，“候选方案对比”应将 `UA1123` 标为“当前”且“会错过”，其余按时方案标为“可按时抵达”。随后才出现“分析执行结果”，并转换为“需要处理的问题”。
4. 问题卡应根据实际执行显示 `UA1123`、实际抵达时间、固定汇报时间和晚到时长；初始快照中不得包含 participant-facing `review_prompt`。修复原则输入框初始为空，用户可主动选择“固定日程优先”建议或自行描述。点击“确认修复原则”后，系统建议直接进入范围检查，自定义原则先确认验收条件。
5. 进入“适用范围检查”时，系统应自动执行与用户修复意图相关的全部冻结情况；用户不需要逐项点击“执行”。执行完成后，每张卡片显示现有处理，并提供“查看执行详情”。系统生成的相关 case 不得成为顶部子标签；顶部只显示整轮修复的范围、候选和影响检查状态。打开详情时，应在只读抽屉中显示代表性执行，关闭后仍回到原位置。执行完成前不应要求用户判断范围。
6. 对无固定承诺的常规行程和高额购买确认分别选择“设为回归保护”；界面应说明候选改变结果会被标记为冲突。对极端溢价场景选择“标记为待决边界”；界面应说明系统不替用户决定，候选影响它时发布前需人工确认。
7. 三项范围均确认后，应出现“完成范围确认并生成候选”。任一相关 case 还可选择“不纳入本轮修复范围”：它不得进入候选的行为要求，但修改前后若发生变化仍应提示人工判断；incident 不提供这个选择。全流程不应出现“保持现在”“这里也要改”“我还没决定”等口语化且无后果说明的按钮。
8. 点“完成范围确认并生成候选”。系统应自动完成固定 source-location 检查、生成候选并立即运行修改前后检查，不再停在“确认修改位置”。候选页提供可回看的“相关修改依据”：exact instruction、为什么从这里修改和“完整候选仍需检查”的限制；不得把“临时移除”“最小反转”“确认依据”作为主流程文案。展开详情后，才显示“不应用这条规则／采用相反处理原则”、case、判别问题、两项结果、变化字段、每种 3 次和稳定性。位置线索不构成业务确认；返回修改任一范围判断后，旧 cue/manifest 必须失效并重新执行。
9. 检查 exact diff：只要 manifest 含 Change，服务端不得接受与原 Skill 逐字相同的候选；`text` 中重复写入“1. / 2. / …”不能被当作修改，被定位的旧冲突条款也不能原样保留后再追加相反句。完整列表连续 no-op 时应触发基于同一 manifest 和 source-location evidence 的定点模型改写，并在 `generation_validation.recovery` 留痕；定点改写仍失败才明确报错。任何恢复路径都不得读取 reference Skill 或隐藏 case。有效候选的主区应以双行号展示原版本与候选版本，删除行使用红色 `−`、新增行使用绿色 `+`，未修改行保持中性；每个修改块还应关联 commitment。也可直接编辑已经生成的候选，此时 author 应变成 `owner`，AI rationale 被清除；在 scope 之前直接调用编辑 API 必须返回 409。
10. 在工作区连续执行“确认修复原则 → 确认适用范围”，系统自动完成后续候选生成与修改影响检查。既有卡片不应在状态更新时重新淡入闪烁；新出现的区块应平滑滚动到可见位置。手动向上滚动后，后续后台更新不得把阅读位置强行拉回底部。
11. 自动检查完成后，每个情况应分别显示修改前/候选版本。默认先显示两侧对齐的结构化事实，变化字段应高亮并在下方汇总；长说明默认折叠，展开后 `**加粗**`、列表和行内代码应正常渲染，任意 HTML 只能作为文本显示。Change 和 Preserve 的 mismatch 单独报告；Unresolved 若变化，应显示“修复进入了尚未授权的边界，需要你判断”，不能显示为失败或成功。技术请求失败必须显示为“执行失败”并提供“重新运行检查”，不能伪装成证据不足或允许发布。
12. 有 warning 时，主按钮应区分“待判断”、“冲突”或“证据不足”，而不是统称为风险。点击后应在工作区出现“发布决定”，逐项解释问题并要求主动确认；未全部确认或未填写发布说明时，“确认发布新版本”不得可用。只有证据不足时才显示“补充执行证据”，并以原/候选每侧 5 次重新比较；单纯的待判断边界不应建议重复执行。对某项 warning 点候选块检查时，单一修改自动执行，多项修改先在页面内选一条；结果要标明完整候选 baseline、3 次“不采用这项修改”的检查和“不能证明唯一因果”。发布成功后，应同时出现带新版本号的短暂提示和持续发布回执，顶部及右侧状态不得回退为“还没有候选修改”。
13. 在 release review 点“继续修改候选”：上一 candidate 与四项 verdict 应进入 `candidate_rounds`，新起草收到失败/待判断反馈但沿用同一 scope 与 source cue。点“调整评审范围”：上一 candidate/evidence 应归档，页面显示新 review round；至少保存一项范围调整后，系统重新生成 cue、候选与对比。导出中旧情况带 `superseded_at`，不能混入当前 comparison 或 release。
14. 再打开新评审，已发布的 Change/Preserve 应存在于导出包的 `regression_cases`；Unresolved/Excluded 不应自动成为回归断言。
15. 最后点击右上角“重置”。系统应清空本轮并回到工作流选择，不得自动执行任何事故；重新选择场景并接受工单后才运行初始版本。左侧指令列表不应再出现“检验影响”按钮。
16. 点击左侧“切换工作流”，选择另一个场景并接受工单。系统应进入另一场景；再次选择已打开的场景应恢复其原进度，不创建重复 skill，也不重新执行已经完成的事故。正式任务链接中不得显示此入口。

再用一个空白服务打开 `/chat`：加载期间必须立即显示恢复提示，不能留下空白工作区；无活动任务时应显示产品说明和相同的两个工作流选择。选择后必须先显示与结构化条件逐字相同的数据字段和工单 hash，接受工单后才以 3 次运行重放历史事件。运行完成后应先出现可展开的完整发布版 Skill，再叙述同一个 execution-derived 问题，并直接询问这类情况以后应怎样处理；顶部“查看 Skill”应打开临时只读抽屉，包含全部编号指令和工具，关闭后返回原对话且没有持久侧栏。输入类似“有固定承诺时先保证按时抵达，再比较价格”后，服务端应保留 owner 原话并将判断显式绑定 incident case，先生成带 hash 的 scope plan，再自动运行三个已校准情况；会话先回述触发条件、要求/禁止动作与开放歧义，每个 case 都说明它与该规则的关系，并明确标为适用范围反例、既有保护或开放边界，不要求用户输入“列出、打开、运行”等口令。报销场景输入“品类不合规时不应自动入账，需要管理员审核”时，客户礼品只能以“不合规是否限于政策明确受限清单”的定义边界出现，同时指出当前实际工具是主管审批。用户应能在一条回复中接受、覆盖或排除全部相关情况。范围完成后，会话必须说明哪条原指令与问题相关、为什么，并把“查看系统如何判断”作为可展开详情；不得要求用户再回复“确认位置”。同一服务请求随后冻结含 `scope_plan_hash` / `repair_preview_hash` 的 manifest、起草候选并完成四个 case 的修改前后检查。完整候选以可展开的会话项出现；最终回复概括修改和四项 verdict，并只询问发布、调整或暂缓。用户提出“检查候选第 N 条是否导致这个结果”时，必须调用与 workspace 相同的 candidate-block 核心并返回相同限制。发布成功必须出现新版本号、证据已保存和问卷入口。刷新已有任务时，轻量 bootstrap API 应恢复共同评审目标、scope plan、repair preview、review round、历史事件、已记录范围、候选/terminal 状态和当前真实决策点，而不是恢复内部工具口令。重复修正同一 case 必须替换当前判断，而非追加矛盾记录。聊天条件不能出现能力命令目录、持久结构化 scope、可点击 case 导航、结构化 diff 或并排 outcome 面板；导出包的 `chat` 数组应保存 owner 消息、自动 actions、结果、进度和最终回复。演示态顶部应有“切换工作流”：切换到另一场景后开始新对话，再切回时恢复旧场景记录。演示态“重置”应清空当前轮并返回工作流选择，不得自动执行；`study=1` 或 `lockMode=1` 下两个入口都必须隐藏。

普通演示中，从 `/` 右上角切换到 `/chat` 再切回，query string 与服务端评审状态必须保留；刷新 `/chat` 后同一浏览器 session 的消息必须恢复。用 `?lockMode=1` 打开任一条件时，切换入口必须隐藏，结构化条件的“重置”也必须隐藏。

用 `study_assignments.py` 生成至少四位参与者的八项任务，再各取一个 workspace/chat 启动命令。两页都应直接显示相同工单而不显示 scenario picker；正式任务接受前不能关闭工单。接受后记录中的 `skill.study_context` 应包含 participant、condition、period、scenario、task id/hash、`brief_acknowledged_at` 和 `started_at`。随后分别更改 participant、condition、period、scenario 或工单版本再访问同一实例：两页都必须锁定，服务端第二次正式载入必须返回 409，而不是悄悄恢复、切换或覆盖。

发布或暂缓后点击“完成任务问卷”：页面应显示两个未参与修复的新情况及完整输入简报，共要求回答 6 项行为预测和无默认值的信心选择，再完成 8 项体验评分与 6 个任务负荷维度。提交期间才执行 holdouts；完成页不得显示正确答案、prediction score、oracle 或 research holdout。研究目录中的任务文件应保存 frozen artifact、6 项预测评分、每个 prediction case 3 次（共 6 次）prediction runs 与 3 次 blind research runs，且 `archive.stage=completed`。在任务中途停止进程，再执行同一启动命令；页面应恢复原状态，文件不得产生第二份任务或混入另一 participant 的记录。

## 5. 导出包审计

调用 `POST /api/export` 后检查：

- `format` 为 `skillscope/2`；
- `skill.scope_history` 每版有 parent、items 与 hash；
- candidate/release 保存 `input_manifest.hash`、`repair_preview_id/hash`、review round、visible/withheld/excluded cases、model、review temperature 与 agent temperature；
- `case_exposure` 区分 owner 与 candidate author 是否见过情况；
- 每个 scenario run 保存 artifact/snapshot/tool/world/case hashes 和完整 tool trace；
- release evidence 的 case rows 只引用 exact original 和 exact complete-candidate runs；`source_interventions` 只列当前 manifest 冻结的两项 M0，`candidate_interventions` 只列当前 candidate hash 与当前 cases 的 Mp，不能混入历史 preview/candidate probes；
- 每个 case 保存 criterion/evaluator hash、baseline/candidate run IDs 与 candidate reveal time；
- `record_hash` 可重建，warning release 有 owner rationale；
- `candidate_rounds` 保留被替换或丢弃候选及其 comparison；post-reveal scope round 的旧 rows 被 supersede，当前 release 不引用它们；
- questionnaire 保存 terminal decision 时的 exact artifact，公开 payload 不包含 fact key、答案、oracle 或 research holdout；
- questionnaire 包含两个 prediction cases、6 个预测项、每项显式信心以及按 case 分开的 3 次后台执行；分析记录不再导出 selected-answer Brier 或未经定义的 workload mean；
- 事件中存在完整共享语义路径 `task_started → intent_committed → scope_committed → candidate_revealed → comparison_viewed → decision_submitted → task_completed`；原始点击数与聊天轮次不作为跨条件效率指标；
- terminal decision 分类为 `supported_release`、`unsafe_release`、`justified_nonrelease` 或待盲编码的 `nonrelease_requires_rationale_coding`，不能把所有 clear-case defer 自动算错；
- `skill.work_order` 与 `skill.study_context` 保存 participant 实际确认的任务版本、condition、period 和起始时间，analysis row 可直接提取这些字段；
- public API 不暴露 reference skill、oracle 定义或 research holdout。

## 6. 正式实验校准门槛

每个带 oracle 的 case 都必须在冻结 pack 中显式声明 faulty/reference 应通过还是应失败；脚本按这份声明验收，不从 case role 猜测预期。

```bash
DEEPSEEK_API_KEY=... python3 scripts/calibrate_scenarios.py all 20 | tee calibration-model-date.txt
DEEPSEEK_API_KEY=... python3 scripts/calibrate_candidates.py all 20 3 | tee candidate-calibration-model-date.txt
```

正式冻结建议门槛：

- 标为 `faulty: fail` 的组合 oracle pass rate ≤ 0.20；
- 标为 `faulty: pass` 或 `reference: pass` 的组合 oracle pass rate ≥ 0.80；
- 每个组合 modal behavior share ≥ 0.80；
- API/tool error rate 必须单独报告，不能从分母静默删除。

candidate-path 校准还应满足：相同 canonical manifest 下无 unchanged draft 或生成错误、artifact modal share ≥ 0.80、incident 与 owner-visible Preserve 通过率 ≥ 0.80。generator-withheld、prediction 与 research holdout 结果必须报告，用于发现系统性失败或 pilot ceiling，但不能替代真实 participant pilot。

同时归档 model name、temperature、pack hash、代码 commit、运行日期和完整输出。任何模型、prompt、skill、tool schema、fixture 或 oracle 变化都会使旧校准失效。

## 7. User-study go/no-go

只有在以下条件同时满足时才进入正式采集：自动验收通过、两类 N=20 校准通过、两个条件的核心能力和 agent 执行预算审计一致、holdout 泄漏测试通过、问卷与分析计划已预注册、pilot 参与者与正式样本分离。正式采集为每个 participant × period 使用独立进程，并核对自动归档完成。否则修复后重新冻结并校准。
