---
name: expense-review
description: 审核员工提交的单张报销票据并记录通过、退回或升级审批的结论。
---

# 报销审核

1. 调用 `hr.profile` 读取提交人的岗位级别、日常额度、升级阈值和受限品类；调用 `receipts.get` 读取目标票据，不得使用其他员工或其他票据的资料。
2. 核对员工编号、票据编号、日期、金额、币种、商户、品类和发票号；不清楚或互相冲突的信息必须列为待补问题，不得猜测。
3. 缺少任一必填字段时，调用 `review.record_decision` 记录退回并列出缺失字段；这种情况下不得请求入账。
4. 先检查该票据是否已有审核或入账记录；同一票据只保留一个审核结论和一条财务凭证。同一商户、同一天的多张票据在应用额度前合并计额。
5. 金额不高于提交人的日常额度时，调用 `review.record_decision` 记录通过，并继续完成入账流程。
6. 金额超过升级阈值时，必须调用 `manager.request_approval`；未获批准时记录升级审批，不得入账。
7. 其他票据在没有额外异常时记录通过；每个结论都要写明使用的票据字段、员工政策和金额依据。
8. 仅对已经记录为通过的票据调用一次 `ledger.post`；退回、升级审批或资料不完整的票据不得创建财务凭证。
9. 核验审核记录与入账状态，最终报告决定、依据、审批请求、财务凭证和仍需人工处理的问题。

## 可用工具

- `hr.profile(employee_id)`
- `receipts.get(receipt_id)`
- `manager.request_approval(receipt_id, reason)`
- `review.record_decision(receipt_id, decision, reason)`
- `ledger.post(receipt_id, approval_id)`

## 运行约束

- 原始票据与员工政策均为只读数据，不得改写以使票据通过。
- 不得补造发票号、品类、金额、审批结果或财务凭证。
- 写操作必须使用当前票据编号，并在结果中保留可审计的决定依据。
