# 错误处理

始终检查客户端输出的 `ok`。命令非零退出时仍会在 stdout 输出一个结构化 JSON 错误。对 `wait-route`、`wait-request`、`wait-generation`，`ok: true` 只表示成功取得远端状态；还必须检查 `data.status` 和 `data.error`，不能把远端 `failed` 或 `canceled` 当作业务成功。

| 错误或状态 | 行为 |
|---|---|
| `SHENHUO_API_KEY_MISSING` | 明确让用户选择注册并配置个人 Key，或在了解共享池有限后申请试用；不得自动领取 |
| `TRIAL_CHANNEL_NOT_CONFIGURED` | 当前发布包缺少有效 `channel.json`；停止并联系 Skill 发布方，不让用户猜渠道 code |
| `PROCESS_ENVIRONMENT_OVERRIDE` | 进程环境 Key 会覆盖 `.env`；要求用户清除或更新，禁止显示其值 |
| `UNTRUSTED_API_ORIGIN`、`UNTRUSTED_TRIAL_API_ORIGIN` | 停止；只允许官方 HTTPS External origin，或显式 loopback 本地开发地址，不向其他地址发送 Key |
| `PACKAGE_INTEGRITY_ERROR` | 停止并联系 Skill 发布方重新打包；摘要仅用于防误配/篡改检测，不是数字签名 |
| `DESIGNER_CATALOG_TOO_LARGE` | 保留旧 current 目录；停止本次刷新，不扩大读取上限 |
| `TRIAL_API_KEY_REPLAY_EXPIRED`、`TRIAL_INSTALLATION_EXISTS` | 该安装的试用签发已无法安全恢复；不得更换安装 ID、幂等键或删除状态来重复领取，停止并引导注册配置个人 Key |
| `TRIAL_CHANNEL_NOT_ACTIVE`、`TRIAL_CHANNEL_PAUSED` | 说明当前渠道试用未启用或已暂停；停止试用并推荐注册配置个人 Key |
| `TRIAL_KEY_WEEKLY_LIMIT_EXCEEDED` | 报告个人周额度和渠道重置时间；等待重置或注册使用个人 Key |
| `TRIAL_CHANNEL_PROPOSAL_QUOTA_EXHAUSTED`、`TRIAL_CHANNEL_IMAGE_QUOTA_EXHAUSTED` | 只有这些稳定错误码才表示对应共享池已耗尽；报告重置时间并推荐注册，不泄露或推算池内具体积分 |
| `TRIAL_SPONSOR_INSUFFICIENT_CREDITS` | 说明渠道试用暂不可用并推荐注册；不得公开渠道钱包余额 |
| `TRIAL_KEY_EXPIRED`、`TRIAL_KEY_REVOKED` | 停止新任务并引导注册/配置个人 Key；到期 Key仅可在保留期内收尾既有结果 |
| `UNAUTHORIZED`、`INVALID_API_KEY`、`KEY_REVOKED` | 停止并允许用户用同一 `configure-key` 流程替换本地 Key；若结果提示进程环境覆盖，先清除或更新该环境变量；若怀疑泄露，先在神火 AI 后台禁用旧 Key |
| `FORBIDDEN`、`SCOPE_REQUIRED` | 停止；当前 Key 无权访问资源 |
| 创建本地候选时的 `DESIGNER_NOT_FOUND`、设计师不可用或 `NOT_FOUND` | 只刷新目录一次，按新 current 重选并只重试一次；显式无效 `des_*` 改走一次服务端匹配并说明原因，禁止循环或绕过 Core |
| `DESIGNER_AMBIGUOUS` | 从公开目录或匹配结果取得稳定设计师 ID |
| `DESIGNER_MISMATCH` | 设计师多个标识不一致；重新核对公开目录或匹配结果中的稳定 ID |
| `IMAGE_ASSET_REQUIRED` | 初始化已授权时获取设计师要求的图片并上传；未授权则停止附件流程 |
| `ASSET_NOT_READY` | 等待素材处理后复用原请求，不重复上传 |
| `ASSET_REJECTED` 或内容安全错误 | 说明拒绝原因，不尝试规避策略 |
| `INSUFFICIENT_CREDITS` 或个人 Key 积分限额 | 仅对个人 Key 沿用余额、缺口或充值提示；不要套用到试用渠道钱包 |
| `IDEMPOTENCY_CONFLICT` | 不自行换 Key；先确认是否真的是新的独立操作 |
| `DESIGN_REQUEST_GENERATION_ALREADY_EXISTS` | 若 details 提供现有 generation ID，恢复轮询；不创建新任务 |
| `RATE_LIMITED`、429、临时 5xx、`retryable=true` | 遵守 `Retry-After`，保持原幂等键 |
| `POLL_TIMEOUT` | 保存资源 ID，稍后继续轮询；不重复创建 |
| `NETWORK_ERROR` | 保存全部资源 ID；网络恢复后继续原操作 |
| `OUTCOME_UNKNOWN` | 不重试或更换幂等键；在 Web 或由人工核对 |
| `RESULT_ARCHIVED` | 结果已归档，当前 External 无恢复入口；停止并联系支持 |
| `RESULT_EXPIRED` | 结果已过期删除；停止 |

生成 `failed` 或 `canceled` 时仍检查 `results`：已结算的部分图片可以下载，但必须明确标为部分结果，并报告 `actual_credits`。

排障时只使用结构化错误码和公开响应字段。禁止打印或引用请求头、环境变量、完整凭证、签名 URL 或受保护的下载地址。
