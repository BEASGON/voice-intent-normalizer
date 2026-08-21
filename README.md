<div align="center">

# 🦊 谐音狐 Voice Intent Normalizer

### 你负责说，谐音狐负责听懂

面向智能体的中文语音意图纠错技能。<br>
识别同音错字、AI 热词、项目术语与个人表达，<br>
减少重复解释、错误执行和无效 Token 消耗。

适配 Codex、OpenClaw、WorkBuddy 等桌面智能体。

[![CI](https://github.com/BEASGON/voice-intent-normalizer/actions/workflows/ci.yml/badge.svg)](https://github.com/BEASGON/voice-intent-normalizer/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/BEASGON/voice-intent-normalizer)](https://github.com/BEASGON/voice-intent-normalizer/releases)
[![License](https://img.shields.io/github/license/BEASGON/voice-intent-normalizer)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)

简体中文 · [English](README.en.md)

</div>

---

## 你可能遇到过这些情况

你对着 AI 说：

> 帮我给 OpenClaw 适配这个技能。

语音识别却写成：

> 帮我给 open cloud 适配这个技能。

或者：

| 你真正想说的 | 语音识别结果 |
| --- | --- |
| `OpenClaw` | `open cloud` |
| `Codex` | `code X` |
| `WorkBuddy` | `work body` |
| `会话中的上下文` | `绘画中的上下文` |
| `我说的是设计` | `我说的是不涉及` |

这些内容单独看，可能仍然是一句“正常的话”。普通拼写检查很难发现问题，
智能体也可能按照错误的词语继续理解和执行任务。

## 真正的问题，不只是识别错了一个字

当你只是聊天时，一个错字可能无关紧要。

但当你通过语音让智能体修改代码、搜索资料、调用工具、生成文档，
甚至执行多步骤任务时，一个专业名词识别错误，就可能让整条指令偏离原意。

### 专业词汇不在普通语音词库里

项目名称、行业术语、产品名称、博主昵称和内部缩写，通常不是大众日常使用的词。
即使发音完全正确，语音识别也可能优先选择一个更常见、但含义完全不同的词。

### AI 新词出现得太快

Codex、OpenClaw、WorkBuddy，以及不断出现的新模型、新产品和新框架，
正在快速进入中文语境。这些词可能没有统一的中文读法，也来不及进入传统词库，
中英文混说时尤其容易出错。

### 同音错误不是普通错别字

“会话”和“绘画”都是正确的中文词语。普通检查只能判断一个词是否存在，
却不知道在当前指令和项目中，哪个词才是你真正想表达的意思。

### 每个项目都有自己的语言

不同项目拥有不同的模块名、功能名、客户名、产品名和内部缩写。
同一个发音，在另一个项目中可能代表完全不同的内容。

### 语音输入反而变成了手动校对

使用语音输入，本来是为了更快地表达想法。如果每说一句都要停下来检查、删除、
重新输入，甚至为了迁就机器而刻意放慢语速，语音输入反而比键盘更累。

### 错误会被智能体继续放大

智能体理解错误后，可能继续搜索错误的技术名称、修改错误的模块、生成错误内容，
或者沿着错误方向完成整条任务链。这不只是错别字问题，更关系到任务能否安全完成。

### 每一次误解，还会产生额外的 Token 成本

```text
错误理解
  → 错误检索
  → 错误规划
  → 错误生成
  → 用户重新解释
  → 整条任务重新执行
```

第一次错误回答、后续纠正说明、重复读取上下文和重新执行，都会继续消耗 Token。
任务越长、步骤越多，早期误解造成的无效消耗就越明显。

## 现有方法为什么还不够？

| 现有方法 | 局限 |
| --- | --- |
| 输入法自定义词库 | 不了解当前项目，也很难跟上不断出现的 AI 新词 |
| 普通错别字检查 | 无法发现“每个词都正确，但整句话理解错了”的情况 |
| 让智能体自己猜 | 结果不稳定、不可审计，高风险任务不能依赖猜测 |
| 每次手动修改 | 打断表达节奏，失去语音输入原本的效率 |
| 重复粘贴项目说明 | 占用提示词和上下文，产生额外 Token 消耗 |
| 每次重新告诉 AI | 新会话、新项目和不同智能体之间需要反复解释 |

## 谐音狐补上的一层

谐音狐不替代原来的语音识别。它位于“语音转文字”和“智能体执行任务”之间，
在消息提交后，对已经转写出来的文字再做一次意图检查：

```text
你说出指令
    ↓
语音识别生成文字
    ↓
🦊 谐音狐结合已配置词库检查表达
    ↓
应用纠正 / 询问确认 / 保留原文
    ↓
智能体理解并执行任务
```

它关心的不只是“这几个字有没有写错”，而是结合本次指令和已经配置的词库，
判断用户真正想表达什么。

## 四层词库，让纠错更懂你的场景

- **行业词库**：识别软件开发、产品设计、人工智能等领域中的常用专业词汇。
- **项目词库**：匹配当前项目中的模块、功能、产品名称和内部术语，项目之间相互隔离。
- **个人词库**：保存用户明确创建的个人表达习惯，不从普通对话中偷偷学习。
- **AI 热词库**：提供经过审核的离线热词快照，覆盖容易被转写错误的新名称。

## 不只是更准确，也能减少无效 Token 消耗

谐音狐不是提示词压缩器，也不承诺每次请求都使用更少的 Token。
它减少的是因为“没有听懂”而产生的无效消耗：

- 减少反复解释同一个专业名词；
- 减少错误回答和错误代码生成；
- 减少每次新会话重复粘贴词表；
- 减少智能体沿错误方向继续执行；
- 减少整条任务链被推翻后重新开始。

> 少解释一次，少重做一遍。<br>
> 把 Token 用在完成任务上，而不是用在纠正误解上。

## 谐音狐如何做决定？

谐音狐不会看到相似读音就直接替换。每次判断只会产生三种结果：

| 结果 | 含义 |
| --- | --- |
| `apply` | 有足够依据，采用纠正后的表达 |
| `ask` | 存在重要歧义，先向用户确认 |
| `keep` | 证据不足，保留原来的文字 |

涉及删除、发布、部署等高影响任务时，谐音狐会更加保守，不会擅自猜测。

## 60 秒快速开始

1. 克隆并进入项目：

   ```powershell
   git clone https://github.com/BEASGON/voice-intent-normalizer.git
   cd voice-intent-normalizer
   ```

2. 测试一条中文语音转写文本：

   ```powershell
   python scripts/voice_intent.py normalize --text "帮我适配 open cloud 的技能" --domain ai --json
   ```

3. 安装到 Codex 并检查状态：

   ```powershell
   python scripts/voice_intent.py install --platform codex --json
   python scripts/voice_intent.py doctor --platform codex --json
   ```

## 支持的平台

| 平台 | 接入方式 | 安装行为 |
| --- | --- | --- |
| Codex | 自动 | 安装公开的消息提交钩子 |
| OpenClaw | 隐式 | 在资格检查通过后使用公开的本地技能 CLI |
| WorkBuddy | 手动 | 生成 ZIP，通过 **Skills → Add Skill → Upload Skill** 导入 |
| 通用智能体 | 手动 | 在宿主支持时生成可读取的本地技能胶囊 |

详细的验证、停用和卸载方式请参阅[平台兼容性文档](references/platform-compatibility.md)。

## 创建自己的词语映射

学习是明确且本地的。映射可以保存为跨项目的个人偏好，也可以只用于当前项目：

```powershell
python scripts/voice_intent.py learn --alias "open cloud" --canonical "OpenClaw" --scope personal --json
python scripts/voice_intent.py learn --alias "项目里的说法" --canonical "项目标准名称" --scope project --json
python scripts/voice_intent.py reject --alias "open cloud" --canonical "OpenClaw" --json
python scripts/voice_intent.py undo --json
```

如果正确范围不明确，谐音狐会询问，而不是直接写入映射。

## 隐私与安全

- 个人词库与项目词库保存在本地；
- 不上传语音转写内容、会话历史、个人映射或访问令牌；
- 项目扫描只读取当前直接项目，并遵守扫描排除规则；
- 不从普通对话中偷偷学习；
- 高影响歧义优先询问，而不是直接纠正；
- 发布包只包含经过审核的运行文件。

当前 `v0.1.0` 不启用网络热点更新；新的热点快照只随经过审核的项目版本发布。

## 它不是什么？

谐音狐不是新的语音识别引擎，也不是系统输入法。它不会在说话时实时修改输入框，
而是在消息提交后，帮助智能体解释已经转写出来的文字。

纠错结果表达的是“我会怎样理解这段文字”，而不是“我修改了你的原始输入”。

## 常见问题

- 如果纠错服务不可用或返回无效数据，保留原始文字。
- 如果高影响纠正存在歧义，先回答确认问题，不要依赖猜测。
- 安装后运行 `python scripts/voice_intent.py doctor --platform <名称> --json`。
- WorkBuddy 需要通过可见的 Skills 页面上传生成的 ZIP；项目不会检查未公开的客户端文件。

## 项目愿景

AI 新词正在快速进入中文语境，但传统语音词库常常跟不上变化。
谐音狐希望成为中文语音输入与智能体之间的一层轻量适配器：

> **你负责说，谐音狐负责听懂。**

让专业词汇不再成为语音输入的障碍，让每一次指令都更接近你真正想表达的意思。

## 开发

```powershell
python -m pip install -e .[dev]
python -m pytest -v
python -m ruff check src tests
python -m build
```

贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，
安全问题请通过 [SECURITY.md](SECURITY.md) 中的方式报告。

## 开源许可

本项目采用 [Apache-2.0](LICENSE) 开源许可证。
