# Codex / Grok CLI 技能

两个技能通过原生协议监督外部 CLI，显示实际工具进度，并转交问题、审批、取消和会话续接：

| 仓库目录 | 技能名 | 默认协议 | 兼容入口 |
| --- | --- | --- | --- |
| `grok/` | `grok` | Grok ACP | Grok 直接 headless 调用 |
| `codex-cli/` | `codex-cli` | Codex app-server stdio | `codex exec --json` |

在 Codex App 中，监督助手把请求转交到当前对话，收到回答后回传给对应运行器。运行器不会自行创建 App 任务卡或打开 CLI 原生窗口。具体任务仍需持续监督；普通命令内部的 stdin 提示不属于这两个协议的交互通道。

使用时读取对应 `SKILL.md`。运行器的 `status` 显示工具、待回复请求和最后活动时间，`events` 支持游标增量读取。没有新事件只表示暂未观察到活动，不能据此认定卡死。

## 用 skills CLI 安装

这是两个独立的 Agent Skills，通过 GitHub 上的 [skills CLI](https://github.com/vercel-labs/skills) 安装，**不是** npm 包。不要对本仓库执行 `npm install` 或 `npx DJB-Developer/codex-grok-cli-skills`。当前 `skills` CLI 1.5.23 需要 Node.js 22.20+，使用其他版本时以该版本的 `engines` 要求为准。

安装两个技能：

```bash
npx skills add DJB-Developer/codex-grok-cli-skills
```

只装其中一个。选择依据是 `SKILL.md` 的 `name`，不是仓库目录名：

```bash
npx skills add DJB-Developer/codex-grok-cli-skills@grok
npx skills add DJB-Developer/codex-grok-cli-skills@codex-cli
```

等价写法：`--skill grok`、`--skill codex-cli`。`@grok-cli` 或 `--skill grok-cli` 不会匹配 Grok 技能。完整 GitHub URL 与 `owner/repo` 简写等价。

先查看、不安装：

```bash
npx skills add DJB-Developer/codex-grok-cli-skills --list
```

常用选项：

- `-g`：安装到用户目录，跨项目可用
- `-a <agent>`：指定目标 Agent（如 `claude-code`、`codex`、`grok`、`cursor`）
- `-y`：跳过确认。在 Agent 内运行时会自动非交互，并安装当前检测到的 Agent
- `--all`：全部技能装到全部 Agent

### 安装结果

CLI 按技能名创建目录，并把该技能目录完整复制进去（`SKILL.md`、`scripts/`、`references/`、`tests/`）。不需要仓库根目录的其他文件，也不需要另一个技能。

| 仓库目录 | 技能名 | 安装目录名 |
| --- | --- | --- |
| `grok/` | `grok` | `grok/` |
| `codex-cli/` | `codex-cli` | `codex-cli/` |

默认项目范围：canonical 副本在当前仓库的 `.agents/skills/<技能名>/`，并按所选 Agent 链接或复制到对应目录。`-g` 写到用户目录，canonical 为 `~/.agents/skills/<技能名>/`。常见 Agent 目录：

- Claude Code：`~/.claude/skills/<技能名>/`（全局）或 `.claude/skills/`（项目）
- Codex：`~/.codex/skills/<技能名>/`（全局）或 `.agents/skills/`（项目）
- Grok Build：`~/.grok/skills/<技能名>/`（全局）或 `.grok/skills/`（项目）
- Cursor 等通用 Agent：项目 `.agents/skills/`，全局见 [Supported Agents](https://github.com/vercel-labs/skills#supported-agents)

已经手动复制到旧目录 `~/.agents/skills/grok-cli` 的副本不会被这条命令覆盖；`npx skills add` 会创建规范目录 `grok/`。迁移时可在确认旧副本不再使用后删除旧目录。

skills.sh 没有单独的发布步骤。公开仓库被 `npx skills add` 安装后，可通过安装遥测出现在 [skills.sh](https://skills.sh)；本仓库不需要 `package.json` 或根目录 `SKILL.md`。不要在仓库根目录添加 `SKILL.md`，否则 CLI 会只发现那一个技能。

## 维护与手动复制

每个 skill 目录都是独立发布单元。运行时只使用本目录的脚本、引用和测试；修改 Grok 或 Codex 的实现时，直接修改对应 skill 的 `scripts/` 文件。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s grok/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex-cli/tests -v
```

不使用 skills CLI 时，分别复制 `grok/` 或 `codex-cli/` 的完整目录到技能目录。两个仓库目录都与 `SKILL.md` 的 `name` 一致。

协议版本、真实 CLI 验证范围和交互响应格式见各技能的引用文档。假协议测试、真实 CLI 验证和任务本身的验收是不同证据。
