# OpenClaw 安装

此适配器通过 OpenClaw 的公开命令行安装 `voice-intent-normalizer`，用于帮助
Agent 理解已经提交的中文语音转写文本；它不会修改宿主输入框中的实时语音文字。

默认使用全局范围：

```powershell
python scripts/voice_intent.py install --platform openclaw --no-auto-update --json
```

传入 `--workspace <工作区目录>` 时，安装仅使用该工作区范围，适合不希望影响同机
其他 OpenClaw 工作区的情况。适配器会调用 `openclaw skills check --json`，并且只在
OpenClaw 报告该技能可用（eligible）后才报告安装成功。

OpenClaw 可能需要新建会话或刷新技能列表后才会发现新技能。适配器不读取、合并或声称
能够访问 Codex、WorkBuddy 或其他 OpenClaw 会话的历史；本次会话的上下文仅由 OpenClaw
按其公开行为提供。

对于本地路径或 Git 来源，OpenClaw 的更新命令不会追踪安装来源，因此适配器会在重新安装
前备份自己的状态记录。卸载时优先使用检测到的 OpenClaw 官方卸载命令；如果该版本没有
该命令，适配器只会移除已验证属于自己的目标，否则会给出手动处理提示。默认卸载不会删除
本项目共享的个人词库、项目词库、偏好或热词数据。
