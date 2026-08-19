# ShenhuoAI Skills

神火 AI 的公开 Agent Skill 发布仓库。安装 `shenhuoai` 后，兼容 Agent Skills 规范的 Agent 可以通过神火 AI 的虚拟设计师完成图片设计：理解 Brief、匹配或选择设计师、补充必要信息、生成最终提示词，并在用户明确要求时生成和下载图片。

> 本仓库只声明兼容 Agent Skills 规范，不代表支持所有 Agent 软件。不同 Agent 的 Skill 安装位置和调用方式可能不同，请同时参考对应产品文档。

## 能力

- 根据设计需求匹配合适的神火 AI 虚拟设计师，也可按公开名称、ID 或擅长方向选择特定设计师。
- 在需要时向用户追问，整理 Brief，并返回可用于生成的最终提示词。
- 经一次性授权后上传用户主动提供的附件和图片，用作设计素材。
- 在用户明确要求生图后发起生成、轮询结果并安全下载到本地。
- 支持个人 API Key，以及共享免费额度有限的 GitHub 渠道试用 Key。

## 使用前提

- Python 3.10 或更高版本。
- 可以访问神火 AI 官方 HTTPS 服务。
- 使用支持 Agent Skills 规范的 Agent 软件。

官方入口：

- 神火 AI：[https://www.shenhuoai.com](https://www.shenhuoai.com)
- 个人 API Key：[https://www.shenhuoai.com/home/api-keys](https://www.shenhuoai.com/home/api-keys)
- External API：`https://api.shenhuoai.com/external/v1`
- Agent Skills 规范：[https://agentskills.io](https://agentskills.io)

## 安装

### 方式一：Codex `$skill-installer`

在 Codex 对话中输入：

```text
$skill-installer https://github.com/chenyth/shenhuoai-skills/tree/main/skills/shenhuoai
```

安装完成后，`shenhuoai` 会在下一轮对话中可用。

### 方式二：Vercel Labs `skills` CLI

以下命令使用 [Vercel Labs 的第三方 `skills` CLI](https://github.com/vercel-labs/skills)，不是神火 AI 或各 Agent 官方安装器。需要先安装 Node.js 18+ 和 npm。

Codex：

```bash
npx skills add chenyth/shenhuoai-skills --skill shenhuoai --agent codex --copy --yes
```

Claude Code：

```bash
npx skills add chenyth/shenhuoai-skills --skill shenhuoai --agent claude-code --copy --yes
```

Cursor：

```bash
npx skills add chenyth/shenhuoai-skills --skill shenhuoai --agent cursor --copy --yes
```

这些命令默认按 CLI 的项目级规则复制安装并跳过交互式确认。如需全局安装，可根据该 CLI 文档添加 `--global`。

### 方式三：手动复制

下载或克隆本仓库，然后把整个 `skills/shenhuoai` 目录复制到 Agent 对应的 Skill 目录中：

```bash
git clone https://github.com/chenyth/shenhuoai-skills.git
cp -R shenhuoai-skills/skills/shenhuoai <你的 Agent Skill 目录>/shenhuoai
```

不要只复制 `SKILL.md`；客户端脚本、渠道配置和参考文件也必须一并保留。

## 首次配置

第一次使用时，Skill 会让你明确选择凭证方式：

1. **个人 API Key（推荐）**：注册神火 AI 账号，并在 [API Key 页面](https://www.shenhuoai.com/home/api-keys)创建自己的 Key。注册账号可获得平台当前赠送的免费积分，具体以网站展示为准。
2. **GitHub 渠道试用 Key**：无需先创建个人 Key，但个人周额度以及渠道的提案、生图共享免费池都有限，额度用完后需要等待渠道重置或改用个人 Key。

配置个人 Key 或领取试用 Key 时，Skill 会一次性询问附件和图片使用授权。授权后，你以后主动提供给该 Skill、用于神火设计任务的附件和图片可以发送给神火处理，不再逐文件重复询问；不同意授权仍可使用不上传文件的纯文本流程。

个人 Key 和试用 Key 都由客户端写入 Skill 目录下权限为 `0600` 的 `.env`，不会作为仓库文件提供。

## 使用示例

安装后可以在对话中直接调用：

```text
$shenhuoai 帮我为一款低糖气泡水设计一张 3:4 的电商主图。
```

```text
$shenhuoai 我想找擅长人物发型预览的虚拟设计师，使用我提供的照片生成四种方案。
```

```text
$shenhuoai 使用我给出的完整提示词直接生成一张 1:1 图片。
```

Agent 会按 Skill 工作流程检查账号、说明素材处理方式、匹配或选择设计师，并在需要用户决定或补充信息时停下来询问。

## 凭证与隐私安全

- `.env` 必须保持 `0600` 权限，不要提交到 Git，也不要复制、打包或分享整个已配置的 Skill 目录。
- 不要在 GitHub Issue、日志、截图、聊天转发或命令行参数中粘贴 API Key、Authorization Header 或试用 bootstrap token。
- 如果怀疑 Key 泄露，请立即在神火 AI 后台禁用该 Key，并创建新 Key 替换。
- 只允许客户端向包内声明的神火 AI 官方 HTTPS origin 发送携带 Key 的请求。
- 用户文件仅应在已经给予一次性授权、且确实用于当前神火设计任务时上传。

## 包完整性

正式渠道包包含 `package-integrity.json`，用于发现文件误配或意外篡改。它是可公开复算的摘要清单，**不是数字签名**，也不能代替操作系统权限、HTTPS、可信发布来源或服务端鉴权。

## 许可证

本仓库使用 [MIT License](LICENSE)。神火 AI 在线服务的使用仍受相应服务条款与平台规则约束。
