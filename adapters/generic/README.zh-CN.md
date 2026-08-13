# 通用 Agent Skills 安装

通用适配器会把 `voice-intent-normalizer` 技能安装到你选择的技能目录。它用于纠正 Agent 对已提交的中文语音转写指令的理解，不会修改说话时输入框中的文字。

命令和用户流程保持不变：

```powershell
python scripts/voice_intent.py install --platform generic --output-dir <技能目录> --json
python scripts/voice_intent.py doctor --platform generic --json
python scripts/voice_intent.py uninstall --platform generic --output-dir <技能目录> --json
```

安装和升级只会在一个完整版本已写入、校验并通过运行检查后激活它。普通升级不改写宿主可见的稳定技能入口；如果新版本激活失败，适配器会保留或自动恢复上一个完整版本，不会暴露混合版本。

`doctor` 会检查技能入口、当前版本、上一版本和共享数据访问。如果检测到第三方程序替换或添加了受管内容，安装器会停止自动修改并报告需要手动处理。请先确认冲突内容的归属并备份需要保留的文件，再重新运行官方命令。

卸载会先停用当前技能，再删除已验证属于该适配器的完整技能单元和私有版本。默认保留本机共享的个人词库、项目词库、偏好和热词数据，也不会影响其他适配器。

通用 Agent Skills 宿主是否能根据 `SKILL.md` 自动发现或隐式调用技能，取决于该宿主的公开能力。未获得宿主确认时，本适配器只报告“手动调用”，不会把描述匹配说成自动触发，也不安装严格 Hook。
