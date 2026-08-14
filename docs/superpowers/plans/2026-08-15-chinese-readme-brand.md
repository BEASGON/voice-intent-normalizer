# 谐音狐中文 README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 GitHub 根 README 改为已确认的“谐音狐”中文产品介绍，同时完整保留英文版并维持发布文档验证。

**Architecture:** `README.md` 作为中文默认入口，`README.en.md` 保存当前英文旅程；`tests/test_release_docs.py` 先定义双语文档的最小发布契约，再写入文档使其通过。修改不影响运行时代码、词库或适配器。

**Tech Stack:** Markdown、Python 3.10+、pytest、Ruff、GitHub Actions

## Global Constraints

- 中文昵称固定为“谐音狐”，英文名称固定为 `Voice Intent Normalizer`。
- 主口号固定为“你负责说，谐音狐负责听懂”。
- 不宣称实时修改输入框、读取全部会话历史、静默学习、自动联网热点更新或必然节省 Token。
- 根 README 保持真实的 Codex、OpenClaw、WorkBuddy、Generic 接入方式和现有 CLI 命令。
- 不新增 Logo、横幅或品牌图片。
- 不修改或暂存五个受保护的预先存在修改文件。

---

### Task 1: 定义双语 README 发布契约

**Files:**
- Modify: `tests/test_release_docs.py`
- Test: `tests/test_release_docs.py`

**Interfaces:**
- Consumes: 当前发布测试使用 `ROOT / "README.md"` 读取公开文档。
- Produces: 中文主 README 的标题顺序契约，以及英文 README 存在并保留原英文旅程的契约。

- [ ] **Step 1: 写入失败测试**

将 `test_readme_follows_the_public_release_journey` 改为读取中文主 README，并使用以下中文标题顺序：

```python
headings = (
    "## 你可能遇到过这些情况",
    "## 真正的问题，不只是识别错了一个字",
    "## 谐音狐补上的一层",
    "## 60 秒快速开始",
    "## 支持的平台",
    "## 创建自己的词语映射",
    "## 隐私与安全",
    "## 常见问题",
    "## 开发",
)
```

新增 `test_english_readme_preserves_the_release_journey`，读取 `README.en.md` 并验证原英文标题顺序：

```python
headings = (
    "## the problem",
    "## post-submission boundary",
    "## 60-second quick start",
    "## platform compatibility",
    "## correction receipt",
    "## natural-language learning",
    "## privacy",
    "## hotword updates",
    "## troubleshooting",
    "## development",
)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
python -m pytest tests/test_release_docs.py -q -k "readme"
```

Expected: FAIL，因为 `README.en.md` 尚不存在，且根 README 尚无中文标题。

### Task 2: 写入中文主 README 并保留英文版

**Files:**
- Create: `README.en.md`
- Modify: `README.md`
- Test: `tests/test_release_docs.py`

**Interfaces:**
- Consumes: 设计说明中的命名、文案边界和现有 CLI 命令。
- Produces: GitHub 默认展示的中文 README，以及可从语言链接访问的英文 README。

- [ ] **Step 1: 保存英文 README**

将修改前 `README.md` 的 UTF-8 内容原样保存为 `README.en.md`，并在英文首屏加入返回中文主 README 的语言链接：

```markdown
[简体中文](README.md) · English
```

- [ ] **Step 2: 写入中文 README**

根 README 使用已批准的介绍文案，并在后半部分包含这些可执行内容：

```powershell
python scripts/voice_intent.py normalize --text "帮我适配 open cloud 的技能" --domain ai --json
python scripts/voice_intent.py install --platform codex --json
python scripts/voice_intent.py doctor --platform codex --json
```

平台表必须准确列出 Codex 自动、OpenClaw 隐式、WorkBuddy 手动、Generic 手动；学习示例必须保留 `learn`、`reject`、`undo`。

- [ ] **Step 3: 运行测试并确认 GREEN**

Run:

```powershell
python -m pytest tests/test_release_docs.py -q -k "readme"
```

Expected: PASS。

- [ ] **Step 4: 运行完整文档发布测试**

Run:

```powershell
python -m pytest tests/test_release_docs.py -q
```

Expected: PASS，包含文档、隐私、平台烟雾和构建归档检查。

### Task 3: 回归验证并提交

**Files:**
- Modify: `README.md`
- Create: `README.en.md`
- Modify: `tests/test_release_docs.py`
- Create: `docs/superpowers/plans/2026-08-15-chinese-readme-brand.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的双语文档。
- Produces: 可发布、可审计的中文 GitHub 首页提交。

- [ ] **Step 1: 运行 Ruff**

```powershell
python -m ruff check tests/test_release_docs.py
```

Expected: `All checks passed!`

- [ ] **Step 2: 运行完整测试**

```powershell
python -m pytest -q
```

Expected: 所有支持当前平台的测试通过，只有既有平台条件跳过。

- [ ] **Step 3: 检查差异和保护范围**

```powershell
git diff --check
git status --short
```

Expected: 文档改动无空白错误；五个预先存在的受保护文件仍保持未暂存且内容未被本任务改变。

- [ ] **Step 4: 提交精确范围**

```powershell
git add README.md README.en.md tests/test_release_docs.py docs/superpowers/plans/2026-08-15-chinese-readme-brand.md
git commit -m "docs: introduce Xieyinhu Chinese README"
```

- [ ] **Step 5: 推送当前分支并确认 CI**

```powershell
git push
```

Expected: 推送成功，GitHub Actions 文档、测试和跨平台检查通过。
