# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**Test-claude3** — 这是一个以配置为核心的项目模板仓库。项目本身不含业务代码，它的价值在于 `.claude/` 目录中的可复用基础设施（Hook + 技能 + 行为规则），可以被复制到其他项目中快速引导开发环境。

## 可用技能

| 技能 | 触发方式 | 用途 |
|------|----------|------|
| `/code-review` | 手动（`/code-review` 或 "审查代码"）| 代码审查：正确性、安全性、代码质量、性能四维检查 |
| `/format-docx` | 自动（提及 .docx / Word 文档时）或手动 | Word 文档生成：标题位置、页码格式、模板清理 |
| `/memory-worker` | **自动**（每次完成主要任务后自动运行，无需用户提醒） | 会话记忆管理：总结关键事件，更新 memory 文件 |
| `/project-init` | 手动 | 一键初始化：`git init` → `.gitignore` → `.venv` → `CLAUDE.md` |

## Code Quality

写完 MATLAB (.m)、Python (.py)、JavaScript (.js) 代码后，**必须先运行验证**，确认无误后再呈现最终结果。

- Python: `python -m py_compile <file>`
- JavaScript: `node --check <file>` 或 `npx eslint <file>`
- MATLAB: `matlab -batch "run('<script>')"`，或至少人工检查维度匹配和函数位置

Hook `.claude/hooks/py_syntax_check.py` 会在每次 Edit/Write `.py` 文件后自动运行 `py_compile`。如果 Hook 报告语法错误，先修复再继续。

## Environment Setup

- `pip install` / `npm install` **必须使用项目本地虚拟环境或目录**（`.venv` / `node_modules/`）
- 永远不要全局安装包，除非用户明确要求
- C: 盘空间有限，每次安装前确认目标路径

```bash
python -m venv .venv
.venv\Scripts\activate     # Windows
source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

## Document Generation (.docx)

生成或编辑 Word 文档时，**在开始之前一次性向用户确认所有格式要求**（不要分多次问）：

1. **标题位置**：表格标题在表格**下方**（"表 X：..."），图标题在图**下方**（"图 X：..."）
2. **页码格式**：目录用罗马数字、正文用阿拉伯数字，还是统一格式？不要自己假设
3. **模板清理**：打开已有 .docx 时，先移除隐藏 SDT 元素和旧 TOC，再插入新内容

完整规则见 `/format-docx` 技能。

## GitHub 远端操作

本项目已接入 GitHub MCP，远端仓库操作**优先使用 MCP 工具**，无需手动配置 git remote 或 personal access token。

### 常用操作映射

| 操作 | MCP 工具 | 说明 |
|------|----------|------|
| 推送文件到远端 | `push_files` | 批量推送文件变更到指定分支，替代 `git push` |
| 创建/更新单个文件 | `create_or_update_file` | 直接在 GitHub 上创建或更新文件 |
| 删除文件 | `delete_file` | 从 GitHub 仓库删除文件 |
| 创建新仓库 | `create_repository` | 在 GitHub 上创建新仓库（用于备份或新建项目） |
| Fork 仓库 | `fork_repository` | Fork 现有仓库到自己的账号 |
| 创建分支 | `create_branch` | 从指定 SHA 创建新分支 |
| 创建 PR | `create_pull_request` | 发起 Pull Request |
| 合并 PR | `merge_pull_request` | 合并 PR |
| PR 审查 | `pull_request_review_write` | 提交 PR Review |
| 搜索代码 | `search_code` | 在 GitHub 上全局搜索代码 |
| 读取文件 | `get_file_contents` | 从 GitHub 仓库读取文件（无需本地克隆） |
| 查看 Issue/PR | `issue_read` / `pull_request_read` / `list_issues` | 查看和管理 Issue / PR |

### 备份工作流

当用户要求"备份到 GitHub"或"创建远端备份"时：

1. `get_me` — 确认当前登录用户
2. `create_repository` — 创建备份仓库（命名建议：`backup-<project>-<date>`）
3. `push_files` — 将当前工作推送到备份仓库

### 注意事项

- MCP 工具直接操作 GitHub API，**无需本地 `git remote` 配置**
- `push_files` 支持单次批量推送多个文件，比逐文件推送更高效
- 使用 MCP 前，先用 `git status` 确认本地工作区状态
- 复杂的 Git 操作（rebase、cherry-pick、conflict resolve）仍用 git CLI
- MCP 操作会触发 GitHub Actions / webhooks（如有配置），注意 CI 消耗

## Debugging Notes

- `uvicorn --reload` / `Flask debug` 只监听 `.py` 文件。修改 `.js` / `.css` / `.html` 后需**手动重启服务器 + 清除浏览器缓存**（Ctrl+Shift+R）
- 修改不生效 → 先问自己：服务器重启了吗？缓存清了吗？
