# Codex / Grok CLI 技能

两个技能通过原生协议监督外部 CLI，显示实际工具进度，并转交问题、审批、取消和会话续接：

| 技能 | 默认协议 | 兼容入口 |
| --- | --- | --- |
| `grok-cli`（技能名 `grok`） | Grok ACP | Grok 直接 headless 调用 |
| `codex-cli` | Codex app-server stdio | `codex exec --json` |

在 Codex App 中，监督助手把请求转交到当前对话，收到回答后回传给对应运行器。运行器不会自行创建 App 任务卡或打开 CLI 原生窗口。具体任务仍需持续监督；普通命令内部的 stdin 提示不属于这两个协议的交互通道。

使用时读取对应 `SKILL.md`。运行器的 `status` 显示工具、待回复请求和最后活动时间，`events` 支持游标增量读取。没有新事件只表示暂未观察到活动，不能据此认定卡死。

## 维护与安装

每个 skill 目录都是独立发布单元。运行时只使用本目录的脚本、引用和测试；修改 Grok 或 Codex 的实现时，直接修改对应 skill 的 `scripts/` 文件。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s grok-cli/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex-cli/tests -v
```

安装时分别复制 `grok-cli/` 或 `codex-cli/` 的完整目录到技能目录，不需要仓库根目录的其他文件，也不需要另一个 skill。

协议版本、真实 CLI 验证范围和交互响应格式见各技能的引用文档。假协议测试、真实 CLI 验证和任务本身的验收是不同证据。
