# SkillScope

SkillScope 是一个面向 reusable agent skill 的行为补丁评审原型。它让 workflow owner 从一次真实失败出发，先确认修复目标，再设置回归保护与待决边界，最后审查 skill 文本修改和完整候选在配对执行中的实际影响。

系统支持两种运行路径：任意 skill 可使用模型生成后冻结的工具快照；论文中的正式场景使用可复位的本地工具世界。后者把外部服务做成 typed agent tools 和固定数据表，模型仍须逐步发起真实工具调用，事实则由调用轨迹与世界终态计算，不来自模型自述。

系统适用于能够重放或在 sandbox 中执行的工作流。它不认证开放世界 agent，也不会替 workflow owner 推断政策。

## 运行

```bash
export DEEPSEEK_API_KEY=sk-...
python3 server.py
```

默认地址为 `http://127.0.0.1:8000`。可用 `PORT=8775` 修改端口、用 `SKILLSCOPE_MODEL=...` 修改模型；`SKILLSCOPE_REVIEW_TEMPERATURE` 和 `SKILLSCOPE_AGENT_TEMPERATURE` 分别控制起草/路由与工具 agent，正式研究默认都为 0。结构化工作区位于 `/`，能力等价的纯聊天模式位于 `/chat`；演示模式下两页右上角可切换、查看工单并重置。聊天页会自动提供可展开的发布版 Skill 原文，也可从顶部按钮临时打开只读抽屉，但不提供持久 scope 或 case 导航。正常聊天链路由系统主动编排工具：owner 说明事故修复目标、确认或修改相关边界，系统随即生成并检查候选，最后由 owner 决定是否发布，不需要输入“打开 case”“生成候选”等操作口令。相关原规则位置作为可回看的修改依据提供，不构成额外确认门槛。正式实验使用 `study=1` 隐藏切换与重置入口。

正式任务应使用带分配信息的链接，而不是让 participant 自己选择 scenario：

```text
/?study=1&scenario=travel-rebooking&participant=P001&period=1
/chat?study=1&scenario=expense-review&participant=P001&period=2
```

两种链接都会先显示 scenario pack 中同一份“评审工单”，participant 接受后才载入和重放历史事件。`study=1` 要求完整的 scenario、participant 与 period，同时隐藏工作流切换、模式切换和重置；服务端把工单 hash、participant、condition、period 和起始时间写进研究记录。已有正式状态与链接任一字段不一致时，页面会锁定且服务端拒绝覆盖。演示模式下，两种界面都可从“切换工作流”进入另一场景；再次选择已有场景会继续原评审，不会复制记录或重跑事故。

正式采集不要直接运行共享的 `server.py`。先生成四序列平衡的任务表，再按表中命令为每个 participant × period 启动一个隔离进程：

```bash
python3 scripts/study_assignments.py --count 4 --start-port 8800 \
  --data-dir ./study-data --output ./study-data/assignments.csv
# 执行 CSV 中对应任务的 launch_command
```

`run_study_task.py` 会强制配置独立研究目录；每次状态变化后原子覆盖该任务的检查点，问卷完成后把同一文件标为 `completed`。进程中断时重新执行完全相同的命令即可恢复，不需要参与者重新开始。一个进程仍只允许承载一个正式任务。

服务端只依赖 Python 标准库；模型请求使用 DeepSeek 的 OpenAI-compatible chat/tool-calling API。状态变更工具会在隔离工具环境中真实执行并返回正式服务等价的结果结构，但不连接生产账户。

## 产品工作流

1. owner 阅读并接受一份不包含修复答案的业务评审工单；两种研究条件共享相同工单。
2. 系统打开冻结的历史事故，并用原 skill 重放完整工具工作流。
3. owner 从具体执行记录一个行为承诺以及可检查的判据，标记为 Change、Preserve 或 Unresolved；邻近 case 也可明确排除本轮，但仍保留为发布监测，incident 不能排除。
4. 系统先把 owner 的表述整理为触发条件、要求动作、禁止动作与开放歧义，再从已校准的冻结 case 库选择少量邻近情况。每项都说明它与本次规则的关系，并在候选生成前执行和确认。
5. 范围完成后系统自动定位一条相关原指令。默认卡片展示 exact source、为什么从这里修改以及“候选仍需完整验证”的限制；它可以随时回看，但不要求 owner 把诊断线索确认为政策。“查看系统如何判断”才展开“不应用该规则／采用相反处理原则”两种内部检查、每种 3 次的结果与 provenance。
6. 系统以 owner 已确认的 scope 生成不可变 input manifest，逐字复制当前 scope，记录位置线索对应的 $M_0$ hash，并区分 generator-visible、generator-withheld 与 excluded 情况。直接编辑也不能绕过范围确认。
7. AI 起草或 owner 直接编辑候选。系统保存 exact diff、author、scope/manifest/artifact hashes 和 case exposure。
8. 原版本与完整候选在相同世界、工具 schema、时钟和模型配置上各运行 3 次。Change/Preserve 可符合或不符合；Unresolved/Excluded 一旦发生变化，只进入 Needs judgment，不算成功或失败。显式补证才以每侧 5 次重跑；候选块检查在两边都以完整候选为 baseline 运行 3 次。
9. Release、继续修改候选、调整范围、Gather evidence 或 Defer 都留下评审记录。候选修改保留同一 scope；post-reveal 范围修改开启新轮次并重新分析建议位置。发布后的 Change/Preserve 情况自动成为下一版本的 regression assets。
10. 发布或暂缓后进入独立任务问卷：用户先预测冻结候选在两个新情况中的 6 个可观测行为事实，再填写 8 个体验单项与 Raw TLX 维度。提交后系统在后台运行两个 prediction holdouts 和一个 research holdout，且不向用户返回正确答案。

在纯聊天模式中，owner 说出事故应怎样处理后，系统先回述其规则理解，再自动执行意图相关的冻结情况；每项明确区分适用范围反例、既有保护和真正开放的边界。owner 可以在一条回复中确认、覆盖或排除这些情况。系统随后说明哪条原指令与问题相关以及原因，并将内部检查放入可展开详情；范围一旦确认，系统自动生成候选并完成 matched comparison。完整候选以可展开的会话项提供，发布、修改或暂缓仍必须由 owner 明确决定。

完整系统契约见 [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)，研究条件与测量设计见 [STUDY.md](STUDY.md)。

## 可执行场景

| 场景 | 故障 | 主要工具 | 隔离情况 |
|---|---|---|---|
| 差旅中断改签 | 读取了固定日程，却把它当作仅供说明的软信息，最后按最低价选择迟到航班 | 日历、航班搜索、确认、改签 | 事故、低价 withheld Preserve、高额确认 visible Preserve、极端溢价未决、2 个行为预测、blind holdout |
| 报销票据审核 | 低金额快速通道绕过已经读取到的受限类别政策 | HR、票据、经理审批、决定记录、入账 | 事故、常规快速通道 withheld Preserve、缺字段 visible Preserve、礼品未决、2 个行为预测、blind holdout |

每个 pack 固定包含 7 个 case，并带有 faulty Skill、仅供研究者校准的 reference Skill、typed JSON schemas、冻结世界、隐藏 deterministic oracle、公开参考来源与内容哈希。产品端不会返回 reference Skill、oracle 定义或 research holdout；两个 prediction holdout 只会在 terminal decision 后以无答案的输入简报出现。Skill 的生产型结构参考公开 Agent Skills，业务工作流再由公开领域文档校准；场景事实、故障和答案均为本项目原创。

## 证据边界

系统分别保存：

- `M0`：原 skill 的指令干预，用于定位对行为敏感的源区域；
- `A`：确认后的 candidate input manifest 与暴露边界；
- `Sp`：候选 artifact、文本 diff 与 author-reported rationale；
- `Ep`：完整候选和原版本的 matched executions；
- `Mp`：用户按需发起的候选块干预；
- `R`：发布、修改、补证或暂缓决定及其证据哈希。

这些记录可以交叉高亮，但不会互相冒充：`M0` 不能验证新写的候选块，完整候选结果也不能单独归因到某一条文本。

## 自动验收

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py scenario_runtime.py scripts/*.py
python3 scripts/acceptance.py http://127.0.0.1:8000 travel-rebooking 5
python3 scripts/e2e_acceptance.py http://127.0.0.1:8000 travel-rebooking
python3 scripts/e2e_acceptance.py http://127.0.0.1:8000 expense-review
python3 scripts/calibrate_scenarios.py all 1
python3 scripts/calibrate_candidates.py all 1 1
python3 scripts/study_metrics.py export.json
```

`e2e_acceptance.py` 会发布一个测试版本；两个场景必须分别使用空白临时进程。单元测试另外覆盖两个场景 × 两个条件的正式分配契约。两个 calibration 脚本会绕过产品界面读取研究侧信息；正式实验前应运行场景校准 `all 20` 和 candidate-path 校准 `all 20 3`，保存模型、温度、pack hashes 和输出。完整人工验收步骤及门槛见 [ACCEPTANCE.md](ACCEPTANCE.md)。

## 主要文件

- `server.py`：HTTP API、模型调用、scope/candidate/release 生命周期
- `scenario_runtime.py`：scenario pack 装载、typed tool dispatcher、世界复位、事实与 oracle
- `app.html`：条件 A，结构化 behavioral patch review workspace
- `chat.html`：条件 B，共用能力和后端的聊天界面
- `questionnaire.html`：terminal decision 后的行为预测、体验与任务负荷问卷
- `scenarios/packs/`：论文场景及研究侧 reference/oracle
- `tests/` 与 `scripts/`：运行时单测、黑盒验收、闭环验收和场景校准
- `scripts/study_assignments.py` 与 `scripts/run_study_task.py`：正式任务平衡分配、隔离启动、检查点与恢复
- `paper-draft/`：当前 CHI 论文的 LaTeX 源码、参考文献、章节文件与编译版 PDF；其中的占位数据仍需在正式采集后替换

导出格式为 `skillscope/2`，包含 skill 与版本、scope history、manifests、快照、运行、工具轨迹、判据、探测、暴露时间、release records、regression assets、匿名交互事件、chat-only 条件消息，以及任务后问卷与后台 holdout 结果。`study_metrics.py` 将每个任务导出转换为一行 analysis-ready JSON/CSV。
