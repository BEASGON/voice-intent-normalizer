# WorkBuddy 本地技能包

适配器会生成 `voice-intent-normalizer-workbuddy.zip` 和 SHA-256 校验文件。请在
WorkBuddy 的公开界面依次打开 **Skills → Add Skill → Upload Skill**，选择 ZIP 并启用
技能。随后运行测试短语：`打开 OpenClaw 技能并纠正 open cloud`。

当前没有稳定的公开自动导入接口，因此需要在界面中完成上传、启用、停用和卸载。停用技能会
阻止它被调用；卸载已上传的技能不会删除共享个人词库，除非你另外明确选择删除共享数据。

该技能处理的是已经提交的中文语音转写文本，不会修改 WorkBuddy 输入框中的实时语音文字。
