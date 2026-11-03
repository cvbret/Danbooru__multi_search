# 项目协作说明

## 1. 项目目录

所有项目工作都在以下目录完成：

```text
E:\AIProjects\Danbooru__multi_search
```

开始任务前必须阅读：

- `Danbooru桌面浏览器_Demo需求文档.md`
- `Danbooru桌面浏览器_开发阶段与里程碑.md`
- `task.md`
- 本文件 `AGENTS.md`

不得把项目文件写入其他目录。临时验证文件需要放在项目内明确的临时目录，并在任务结束前说明是否保留。

## 2. 项目目标

开发一个 Windows 优先、最终兼容 Windows 和 Linux 的 Danbooru 桌面浏览器。当前技术基线为：

- Python
- PySide6
- Qt Quick/QML
- SQLite
- Windows 10、Windows 11 x86_64 优先
- Ubuntu 22.04、Ubuntu 24.04 x86_64 为首批 Linux 目标

Windows 版本支持 Eagle 本地联调。Eagle 官方没有 Linux 客户端，因此 Linux 版本保留本地自动采集和元数据导出，Eagle 本地联调入口需要显示当前系统不支持。

## 3. 共同工作规则

- 只处理 `task.md` 当前分配的任务。
- 开始工作前检查现有文件和 Git 状态，不覆盖其他 Agent 或用户已有修改。
- 需求不明确、实现会扩大当前阶段范围或需要新增外部服务时，停止修改并报告主 Agent。
- 通用业务模块不得直接调用 Windows 注册表、PowerShell、Linux Shell 或桌面环境命令。
- 操作系统差异必须放入平台适配层。
- 不硬编码盘符、反斜杠、`AppData`、`/home`、`$HOME` 或特定用户名。
- 所有路径使用路径库组合。
- SQLite、JSON、配置和文本文件统一使用 UTF-8。
- 文件命名按照 Windows 的较严格规则清理，同时兼顾 Linux 文件名大小写敏感。
- 应用关闭后不得继续执行查询、下载、采集或 Eagle 导入任务。
- 不实现需求文档排除的小说、加密收藏、后台定时采集、服务器推送和没有数量边界的抓取。
- 不顺带增加分析报告、日志文档、示例项目或与当前任务无关的工具。
- 不执行 `git push`。
- 不使用 `git reset --hard`、`git checkout --` 或其他会丢失未提交修改的命令。

## 4. 主 Agent 职责

主 Agent 负责统合和调度，不代替子 Agent 执行已经分配的职责。

- 根据开发阶段拆分任务。
- 在 `task.md` 中写明当前阶段、任务、范围、验收条件和负责人。
- 把编码任务分配给 `developer`。
- 把测试与审查任务分配给 `reviewer`。
- 测试通过后，把暂存和提交任务分配给 `intergrator`。
- 处理 Agent 之间的阻碍和范围冲突。
- 判定阶段任务是否满足里程碑。
- 每个阶段任务结束后向用户报告完成内容、验证结果、提交编号、剩余问题和下一任务状态。

## 5. developer 职责

`developer` 是编码 Agent，负责实现当前任务。

### 必须完成

- 阅读当前任务和相关需求。
- 修改生产代码、界面、数据库迁移及当前任务明确要求的测试辅助代码。
- 保持业务模块与平台适配模块分离。
- 对新增公共接口补充类型说明和必要注释。
- 对自己修改的范围执行最小必要检查。
- 完成后向主 Agent 报告：
  - 修改的文件。
  - 实现的行为。
  - 已执行的检查及结果。
  - 尚未验证的内容。
  - 需要 `reviewer` 重点检查的边界。

### 不得执行

- 不管理 Git 暂存区。
- 不创建 Git commit。
- 不执行 Git push。
- 不修改当前任务以外的文件。
- 不为了让测试通过而降低验收条件。
- 不在没有主 Agent 指令时修改需求文档、开发阶段文档或 `AGENTS.md`。

## 6. reviewer 职责

`reviewer` 是测试和代码审查 Agent，负责独立验证 `developer` 的结果。

### 必须完成

- 阅读当前任务的验收条件和开发者交接说明。
- 检查修改范围及潜在回归影响。
- 运行与当前任务有关的单元测试、集成测试、静态检查、界面启动检查或安装检查。
- 检查 Windows 与 Linux 共用模块中是否混入平台专用路径或命令。
- 检查取消、失败、重试、应用关闭和数据保存等边界。
- 报告结果时明确列出：
  - 已通过项目。
  - 失败项目。
  - 复现步骤。
  - 相关文件和位置。
  - 是否允许进入 Git 提交阶段。

### 修改权限

- 默认只读取代码和执行检查。
- 只有主 Agent 明确分配“补充或修正测试”时，才可以修改测试代码。
- 不修改生产代码。
- 不管理 Git 暂存区。
- 不创建 Git commit。
- 不执行 Git push。

### 判定规则

- 存在阻断当前任务验收的问题时，结论为“不通过”，并交回 `developer` 修正。
- 只有当前任务的验收条件全部满足后，才可以给出“允许提交”。
- 无法执行的检查必须说明具体原因，不能把未验证内容写成已经通过。

## 7. intergrator 职责

`intergrator` 是统合与 Git 管理 Agent。名称按照项目约定保留为 `intergrator`。

### 执行条件

- 只有收到主 Agent 的明确指令，并确认 `reviewer` 已经允许提交后，才可以操作暂存区和创建 commit。

### 必须完成

- 提交前检查当前分支、工作树状态和完整差异。
- 区分本次任务修改、用户已有修改和无关文件。
- 只暂存当前任务明确列出的文件。
- 暂存后重新检查 staged diff。
- 创建内容明确的本地 commit。
- commit 完成后报告：
  - commit 编号。
  - commit 标题。
  - 已提交文件。
  - 未提交文件及原因。
  - 当前工作树状态。
- 按照主 Agent 指令更新 `task.md` 的提交编号和任务状态；该更新需要包含在同一任务允许的提交范围内，或者单独创建文档提交。

### Git 限制

- 不执行 `git push`。
- 不执行强制推送。
- 不修改远程仓库地址。
- 不使用 `git add -A`、`git add .` 或会包含不明文件的宽范围暂存命令。
- 使用明确文件路径执行 `git add -- <paths>`。
- 不使用 `git reset --hard`、`git clean -fd`、`git checkout --`。
- 不修改或删除用户已有 commit。
- 不执行 `commit --amend`，除非主 Agent 明确要求。
- 发现未说明的修改、合并冲突或敏感文件时停止提交并报告主 Agent。

### commit 标题格式

使用以下格式之一：

```text
chore(m0): initialize project baseline
feat(m1): add basic danbooru browsing
feat(m2): add complex query planner
fix(m3): correct download task recovery
test(m4): cover pool reading order
docs: update project task status
```

## 8. 标准任务流程

1. 主 Agent 在 `task.md` 中建立任务并指定负责人、范围和验收条件。
2. 主 Agent 把任务发送给 `developer`。
3. `developer` 完成代码并提交交接说明，不操作 Git 暂存区。
4. 主 Agent 把开发结果发送给 `reviewer`。
5. `reviewer` 执行测试和审查。
6. 测试不通过时，主 Agent 把问题交回 `developer`，修正后再次交给 `reviewer`。
7. 测试通过后，主 Agent 通知 `intergrator` 检查差异、暂存指定文件并创建本地 commit。
8. `intergrator` 返回 commit 编号和工作树状态。
9. 主 Agent 更新阶段进度并向用户报告。

任何阶段都不能跳过独立测试直接提交。

## 9. task.md 更新规则

- `task.md` 是任务状态的唯一记录。
- 同一时间只设置一个“进行中”的主要任务。
- 每个任务包含编号、阶段、目标、范围、负责人、状态、验收条件和提交编号。
- 状态只能使用：`待开始`、`进行中`、`待测试`、`修正中`、`待提交`、`已完成`、`受阻`。
- `developer` 和 `reviewer` 只报告结果，不自行改变总体阶段状态。
- 主 Agent 决定任务状态变化。
- `intergrator` 只按照主 Agent 指令写入提交编号和提交完成状态。

## 10. 阶段完成报告

每个阶段里程碑完成后，主 Agent 向用户报告：

- 阶段和里程碑名称。
- 已实现功能。
- 测试范围和结果。
- Windows 验证情况。
- Linux 编译或运行验证情况。
- 当前已知限制。
- 本阶段 commit 编号。
- 下一阶段的起始条件。
