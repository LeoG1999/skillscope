# User study design

## 研究问题

实验评估的是 behavioral patch review 交互能否帮助 workflow owner 完成一次可负责的 skill 修复，而不是比较两个 agent 的模型能力。

- RQ1：结构化工作区是否帮助 owner 更准确地理解候选相对原版本改变了什么？
- RQ2：它是否帮助 owner 构造并维持跨情况的 repair scope，包括 Change、Preserve 和 Unresolved？
- RQ3：它是否帮助 owner 基于执行证据作出更合适、可追溯的 release decision，同时保留其政策与发布权威？

## 对照条件

两个条件使用同一个 `server.py`、scenario pack、模型、候选起草与工具执行 prompt、typed tools、冻结世界、case exposure、判据、执行预算和 hidden oracle。条件专用的导航/对话编排属于交互操纵，不改变进入候选生成器的已确认 commitment 或最终证据集合。

### 条件 A：SkillScope workspace

结构化工作区持续呈现 scope、相关修改依据、candidate diff、matched behavioral diff 和 release exits。状态可直接导航，Change/Preserve/Unresolved、非规范性的“排除本轮”以及可见/独立检查边界有明确视觉标识。底层 source-location 操作默认收起；它是可回看的定位线索，不是 participant 必须批准的政策决定。

### 条件 B：chat-only baseline

`/chat` 提供同样的执行、查看候选、记录承诺、相关情况、相关修改依据、候选编辑、候选块检查、影响对比和 release decision 能力，但不把这些内部能力作为用户必须学习的命令。participant 说明事故的业务目标后，共享的 scope planner 先记录对触发条件、要求动作、禁止动作和开放歧义的理解，再运行产品内相关情况；回复逐项说明 case 与 owner 意图的关系，并区分直接边界与独立回归保护。participant 可以用一条自然语言回复确认或覆盖所有判断。范围完整后，服务将相关原指令及其理由作为非阻断线索随结果提供，随后自动冻结 manifest、起草并执行完整候选的 matched comparison；“不应用该规则／采用相反处理原则”的固定检查仅在可展开详情中出现。完整发布版与完整候选 Skill 都以可展开的聊天消息提供，发布版也可从顶部只读抽屉中重看；聊天条件不提供持久 scope 侧栏、可点击 case 导航、结构化 diff 或并排对比。

两边的 behavioral commitment 都必须在 candidate outcome reveal 前由 participant 确认，服务端存储 `judged_at`、`pre_reveal`、`generator_exposure` 和 `candidate_outcome_revealed_at`。因此 scope alignment 在两种条件下都可计算，而不是只在条件 A 存在。

普通演示中两页右上角可互相切换且保留同一轮服务端状态，并各自提供重置；正式实验用 `study=1`（兼容 `?lockMode=1`）隐藏切换与重置入口。条件分配由实验协议决定，不能让 participant 自选、中途跨条件或清除正式任务状态。

## Participant-facing 评审工单

scenario 中 case 的 `task`（例如“重新安排一段行程”）是交给 agent 执行的输入，不是 participant 的研究任务。每个 scenario pack 因此另外提供一份 `work_order`，在任何执行发生前以同一份数据呈现给两个条件。工单只包含：

- participant 的业务角色与发布责任；
- 一次结果被提交人工复核的中性背景，但不提前给出根因或修复答案；
- 目标产物：调查、形成并验证候选修改；
- 共同完成条件：发布，或记录暂缓及理由；
- 相同的建议时长和隔离执行说明。

工单不出现 Change、Preserve、Unresolved、hidden oracle、reference skill 或“正确修改”措辞，也不逐步教 participant 操作。结构化导航和持久状态仍属于条件 A 的实验操纵；任务目标本身不是操纵。每份公开工单有 `task_hash`，participant 点击“接受工单并开始评审”时，服务端将 task、condition、participant、period、scenario 和起始时间一起冻结到导出记录。

正式链接使用预分配任务，例如：

```text
/?study=1&scenario=travel-rebooking&participant=P001&period=1
/chat?study=1&scenario=expense-review&participant=P001&period=2
```

`study=1` 要求 `scenario`、`participant` 和 `period` 完整，并隐藏模式切换与重置入口。已有状态必须与 participant × condition × period × scenario × task hash 全部一致才能恢复；否则界面锁定，服务端也拒绝第二次正式载入，防止串 participant、串条件或覆盖任务。演示模式仍允许选择工作流、查看工单和切换界面。

## 任务与场景分配

采用两个领域：差旅中断改签与报销审核。每个场景都包含：

- 一个能稳定复现的 incident；
- 一个 owner 与候选生成器都可见的 Preserve case，用于检查显式回归保护；
- 一个 owner 可见、但不进入候选生成上下文的 generator-withheld Preserve case，用于检查 repair scope 的迁移；
- 一个没有预设正确政策的 owner-visible Unresolved boundary；
- 两个修复期间完全隐藏、仅在发布决定后向 participant 展示输入的 prediction holdout；
- 一个始终不向 participant 或候选生成器展示的 research holdout。

因此每个 pack 固定为 7 个 case。前 4 个组成真实产品中的一次修复与发布检查；两个 prediction holdout 共提供 6 个行为预测判断，不回流到修改；research holdout 只在问卷提交后于后台执行，用于独立泛化评估。三类隐藏 case 均不会进入候选起草 prompt。

建议采用 2×2 counterbalanced within-subject 设计：每位 participant 完成两个领域、分别使用一个界面，界面顺序与领域映射用 Latin square 平衡。不能让同一 participant 在另一条件再次修复同一个 pack，否则会产生解题迁移。两个任务使用等价但不相同的 onboarding，并限制相同的总时长和模型执行次数。

## 单个任务流程

1. participant 阅读并接受 condition-independent 评审工单。计时和任务日志从接受时开始，而不是从页面打开或模型返回时开始。
2. participant 查看一次历史 incident 及原 skill 的工具执行；执行完成后，系统才从实际 trace/world facts 呈现冲突，不提前显示预制问题或修复答案。
3. participant 可检查数据或重复运行，但正常链路不要求其猜测要测试哪条指令。
4. participant 记录 motivating Change；系统将其编译成 versioned scope plan，并从冻结 case 库返回与触发条件、动作边界或既有保护相关的 Preserve/Unresolved 情况。正式实验的三项已校准 case 在两种条件下保持不变，workspace 逐项呈现，chat 将三项执行合并到一次对话决策中；两边都显示同一份意图解释与关联理由。
5. 系统自动选择一条现有原指令与一个已执行 case，在后台对删除和最小反转各执行 3 次。workspace 与 chat 默认展示 exact source、行为化的选择理由和“仍需完整候选验证”的限制；technical operation、变化字段、固定预算和 provenance 放入“查看系统如何判断”。该信息只定位修改起点，不需要 participant 单独确认；修正范围会使旧记录失效并重新分析。
6. 系统以已确认的 scope 冻结 scope version、位置线索对应的 $M_0$ hash 与 candidate input manifest；其中一个 Preserve case 只进入发布验证，不进入 AI 起草上下文。直接手工编辑也不能绕过范围确认。
7. 系统根据已确认范围生成 AI draft。原版本与完整候选随后在全部 4 个产品内 case 上各执行 3 次；两个条件均自动发起，workspace 分面呈现，chat 合并叙述。只有显式“补充执行证据”才改用每侧 5 次的新一轮比较。
8. participant 可以在同一范围下继续修改候选；上一 candidate 与 comparison 会归档并作为反馈。若要改变政策范围，则显式开启 post-reveal 新轮次，保存一次范围修改并重新生成位置线索。candidate-block 检查在两种条件下都以完整候选为 baseline，各执行 3 次。
9. participant 发布或暂缓后，系统冻结当时的 exact artifact。participant 阅读两个新 case 的完整输入，每个 case 预测 3 个可观测行为事实，并分别报告信心；提交前不执行这些 case。
10. participant 完成 8 项简短体验评分和 6 个任务负荷维度。提交后系统对冻结 artifact 在两个 prediction holdout 与一个 research holdout 上各执行 3 次，保存事实、稳定率和 hidden oracle，但不向 participant 显示答案或分数。

不要把 oracle 或逐项研究目标显示在产品中。产品保持真实 repair workflow；问卷只在任务结束后测量体验与理解。

## Confirmatory 结果指标

为避免把大量相关指标都称为“主要结果”，正式实验预注册三个 co-primary outcomes：

- **Held-out behavior understanding**：两个 prediction holdout 上 6 个可观测事实判断的准确率。每题另记录 0–100 信心，只作描述性校准分析（正确与错误题的平均信心、高信心错误率）；不把所选答案的信心误写成概率分布 Brier score。
- **Behavioral-scope adherence**：最终行为与 candidate reveal 前记录的 Change/Preserve commitments 对齐的比例（导出字段 `scope_alignment` / `candidate_behavior_alignment`）。Unresolved 与 Excluded 不进入分母；若候选改变它们，记为 Needs judgment。
- **Evidence-supported decision**：terminal action 与可见证据及书面理由是否一致。自动分类为 `supported_release`、`unsafe_release`、`justified_nonrelease`；无 warning 的暂缓先标记为 `nonrelease_requires_rationale_coding`，由盲于 condition 的研究者根据原始理由编码后才可能成为 `unnecessary_nonrelease`。Unresolved 经明确说明后发布不自动算错误。

下列机制与安全结果完整报告，但作为 secondary outcomes 或预注册的机制分析：

- **Incident repair correctness**：最终候选是否满足 incident hidden oracle。
- **Explicit preservation**：owner 与生成器均看过的 Preserve case 是否继续满足 oracle。
- **Generator-withheld transfer**：不进入 draft prompt 的 Preserve case 是否继续满足 oracle。
- **Blind generalization**：research holdout 是否通过；该指标仅由后台研究记录计算。
- **Release path**：发布、修改、补证、暂缓、带理由接受 warning，以及关键失败是否被单个平均分掩盖。
- **Evidence traceability**：最终判断能否链接到有效 matched runs、criterion、artifact/world hashes；release record 是否完整。
- **Repair efficiency**：完成时间、模型/工具执行次数、无效重复、scope revisions、candidate revisions。
- **Scope classification**：在有预注册答案的 incident 与两个 Preserve case 上，participant 的 Change/Preserve 分类准确率；真正开放的 boundary 不计入分母，避免把研究者偏好冒充用户政策。

## 次要指标

任务后 8 个单项 7 点评分测量：

- 对“修改实际改变了什么”的理解；
- 对相近情况行为的主观可预测性；
- 对未决边界和剩余风险的觉察；
- perceived control、evidence sufficiency 与 ease of use；
- 两个 manipulation checks：重要信息是否可重新找到、两种条件是否都提供了完成任务所需的 Skill、相关情况和执行结果。任务负荷另外使用 0–100 的六个 Raw TLX 维度；不计算或报告未经定义的总均值。

自报分数是补充证据，不替代行为正确性、scope alignment 或 release calibration。

## 过程指标

跨条件比较只使用共享语义里程碑：`task_started`、`intent_committed`、`scope_committed`、`candidate_revealed`、`comparison_viewed`、`decision_submitted` 与 `task_completed`。界面特有点击和聊天轮次保留作调试，不作为效率优劣的直接比较。质性访谈重点询问 participant 如何解释证据、在哪里保留政策权威、为何接受或拒绝发布。

## 分析与有效性约束

- 两个领域只有两个水平，因此把 condition、domain、period/order 作为固定效应，以 participant 设随机截距；不要把 domain 当作可推广总体的随机效应。二元题使用 mixed-effects logistic model，时间/次数使用与分布匹配的 mixed model。
- 三个 co-primary outcomes 使用预注册的多重比较控制；其余明确标为 secondary/exploratory，并同时报告效应量与置信区间。
- 报告每个关键结果，不以单一平均总分掩盖 safety-critical failure。
- 正式实验前对每个 faulty/reference × case 组合运行至少 20 次，并对真实 candidate drafting path 重复采样。每个 scored case 在冻结 pack 中显式声明 faulty/reference 的预期通过状态；校准据此确认 incident failure、reference repair、显式/withheld preservation、prediction holdout 与 research holdout 的稳定率达到预注册门槛，而不从 case role 猜测预期。
- 两个条件必须使用相同的模型版本、review temperature、agent temperature、tool schemas、世界哈希、固定 run budget 与 oracle；任一版本变化后重新校准。当前默认 review/agent temperature 均为 0。正式 outcome evaluator 只接受可重建的 trace/fact 判据，不使用 LLM semantic judge。
- 明确区分 generator-withheld transfer 与真正 participant-unseen 的 research holdout generalization。
- 记录所有 post-reveal scope edits；它们可以反映学习，但不能继续标为 pre-reveal prediction。

## 研究前冻结项

先用 6–8 人完成可用性与时长 pilot，检查 route 失败、候选 ceiling、prediction 题歧义和两个领域难度是否失衡；pilot participant 不进入正式样本。随后基于最小有意义效应或保守模拟做 power analysis，并冻结被试数、任务时长、运行预算、问卷条目、关键失败的 release 规则、排除标准、统计模型与多重比较策略。

正式采集必须为每个 participant × period 启动隔离实例，不能让并发 participant 或两轮任务共用一个进程。`study_assignments.py` 以四个序列平衡 condition order、domain order 与 condition-domain mapping；`run_study_task.py` 启动单任务实例并强制配置研究目录。服务端在每次状态变化后原子写入该任务的 `skillscope/2` 检查点，问卷完成后将其标记为 `completed`；相同启动命令可恢复中断任务。正式操作仍需在每轮结束时核对文件存在、`archive.stage=completed`、participant/condition/period/task hash 一致，并将研究目录纳入加密备份。

是否设置独立 practice task 在 pilot 后冻结，不是当前系统实现的前置条件。本轮不新增第三个测量外场景；正式任务前先使用同一份简短、condition-matched 的口头说明和 scripted walkthrough，说明 participant 需要承担提出修复目标、确认边界、决定发布三类责任，但不演示两个正式场景的答案或内部工具口令。如果 pilot 显示明显学习效应，再增加不进入分析、单独落盘的短练习。
