# 项目任务与进度

- 项目：Danbooru 桌面浏览器
- 工作目录：`E:\AIProjects\Danbooru__multi_search`
- 当前阶段：第 0 阶段 技术基线与平台结构
- 当前里程碑：`M0 技术基线完成`
- 当前主要任务：`M0-005 Eagle 单图导入技术验证`
- 当前状态：待开始
- 最后更新：2026-11-03

## 1. 阶段进度

- 第 0 阶段 技术基线与平台结构：进行中。
- 第 1 阶段 Windows 基础浏览器：待开始。
- 第 2 阶段 复杂查询与人气排序：待开始。
- 第 3 阶段 本地收藏与下载管理：待开始。
- 第 4 阶段 Danbooru 漫画阅读器：待开始。
- 第 5 阶段 自动采集与 Eagle 联调：待开始。
- 第 6 阶段 Windows Beta：待开始。
- 第 7 阶段 Linux 适配：待开始。
- 第 8 阶段 跨平台发布候选版本：待开始。

## 2. 已完成任务 SETUP-001

### 2.0 状态

已完成。

### 2.1 目标

建立项目文档、Agent 职责和 Git 协作流程，为后续分阶段开发提供统一依据。

### 2.2 工作范围

- 把当前 Demo 需求文档放入项目目录。
- 建立开发阶段与里程碑文档。
- 建立 `AGENTS.md`。
- 建立本文件 `task.md`。
- 创建 `developer`、`reviewer`、`intergrator` 三个子 Agent。
- 由 `reviewer` 检查文档和职责是否完整一致。
- 由 `intergrator` 暂存本次初始化文件并创建本地 commit。
- 不执行 Git push。

### 2.3 负责人

- 调度与验收：主 Agent。
- 文档准备：主 Agent。
- 文档检查：`reviewer`。
- Git 暂存与 commit：`intergrator`。
- `developer`：阅读项目文档并确认编码职责，不修改代码。

### 2.4 验收条件

- 项目目录包含需求文档、开发阶段文档、`AGENTS.md` 和 `task.md`。
- 开发阶段文档包含第 0 阶段至第 8 阶段及对应里程碑。
- `AGENTS.md` 明确三个子 Agent 的权限和交接流程。
- `intergrator` 的职责明确包含管理暂存区和创建 commit，并明确禁止 push。
- `task.md` 能够说明当前阶段、当前任务、状态、验收条件和下一任务。
- `reviewer` 给出允许提交的结论。
- `intergrator` 创建初始化 commit，并返回 commit 编号。

### 2.5 当前进度

- [x] 已确认项目工作目录。
- [x] 已确认目录中存在 Git 仓库。
- [x] 已复制当前 Demo 需求文档。
- [x] 已建立开发阶段与里程碑文档。
- [x] 已建立 `AGENTS.md`。
- [x] 已建立 `task.md`。
- [x] 已创建并初始化 `developer`。
- [x] 已创建并初始化 `reviewer`。
- [x] 已创建并初始化 `intergrator`。
- [x] `reviewer` 已完成检查并允许提交。
- [x] `intergrator` 已创建本地 commit。
- [x] 主 Agent 已完成初始化结果核对并准备报告。

### 2.6 提交信息

- 初始化 commit：`889d5d33f92ed8995f81e77d1ceb8d66097596eb`。
- 初始化 commit 标题：`chore(m0): initialize project documentation`。
- 进度 commit：`20e0783b873d5eab5e2c6a2cd75d3cbf005dd670`。
- 进度 commit 标题：`docs: record setup completion`。
- push：禁止执行。

## 3. 已完成任务 M0-001

### 3.1 任务名称

建立 PySide6 与 QML 应用空壳及跨平台目录结构。

### 3.2 状态

已完成。

### 3.2.1 负责人

- 编码：`developer`。
- 测试与审查：`reviewer`。
- 暂存与本地提交：`intergrator`。

### 3.3 计划范围

- 建立 Python 项目结构。
- 建立 PySide6 与 QML 最小启动窗口。
- 建立 `core`、`danbooru`、`tasks`、`storage`、`integrations`、`platform`、`ui` 模块目录。
- 建立 Windows、Linux 平台接口和空实现。
- 建立基础设置读取和保存。
- 建立 SQLite 数据库连接及第一版迁移。
- 建立 Windows、Linux 基础检查命令。

### 3.4 验收条件

- Windows 可以启动和关闭应用空壳。
- 设置能够保存并在重新启动后读取。
- SQLite 数据库可以建立并记录数据库版本。
- Linux 可以完成导入检查和无界面核心测试。
- 通用模块不包含硬编码操作系统路径。
- 应用关闭后没有残留后台任务。

### 3.5 验证结果

- pytest：14 项通过。
- `compileall`：通过。
- `headless-check`：通过。
- `reviewer` 已完成复测并允许提交。
- Linux 尚未进行实机验证；当前完成了 Linux 平台接口、空实现和 Windows 环境中的针对性测试。

### 3.6 提交信息

- commit：`bbd37159bc83355dcf9fdf56ebc7ff0f26edf82a`。
- commit 标题：`feat(m0): add cross-platform application shell`。
- push：禁止执行。

## 4. 已完成任务 M0-002

### 4.1 任务名称

Danbooru 最小请求与单图显示验证。

### 4.2 状态

已完成。

### 4.3 负责人

- 编码：`developer`。
- 测试与审查：`reviewer`。
- 暂存与本地提交：`intergrator`。

### 4.4 任务范围

- 建立最小 Danbooru API 请求和作品数据模型。
- 请求携带明确的 `User-Agent`、超时、标签、页码和数量参数。
- 每次只请求一条作品，用于技术验证。
- 解析作品编号、预览地址、较大图片地址、原图地址、得分、收藏数量、分级、尺寸和文件格式。
- 在 QML 界面提供标签输入、请求按钮、加载状态、错误提示和单图显示。
- 提供可以自动执行的无界面 Danbooru 请求检查入口。
- 使用模拟响应测试正常、空结果、无效数据和网络错误。
- 当前任务不实现复杂查询、分页、收藏、下载队列和图片落盘。

### 4.5 验收条件

- Windows 可以输入一个标签并请求一条 Danbooru 作品。
- 成功响应能够解析为统一作品模型并向 QML 提供图片地址。
- 空结果、HTTP 错误、超时和无效响应显示明确状态。
- 请求包含项目 `User-Agent`，不在界面线程执行阻塞网络访问。
- 单元测试不依赖 Danbooru 实际网络。
- 可选在线检查只发送最小数量请求，并能在网络不可用时返回明确错误。
- 现有 14 项测试继续通过。

### 4.6 开发交接

- `developer` 已完成作品模型、严格响应解析、QtNetwork 异步请求、QML 单图界面和在线 smoke 入口。
- 自检为 32 项 pytest、`compileall`、`headless-check` 和 QML 离屏加载通过。
- Windows 实际执行 `--danbooru-smoke --danbooru-tags landscape` 成功，返回一条作品。
- `reviewer` 独立验证 32 项 pytest、`compileall`、`headless-check`、离屏 QML 和一次真实最小请求，结论为允许提交。

### 4.7 验证结果

- pytest：32 项通过。
- `compileall`：通过。
- `headless-check`：通过。
- QML 离屏加载：通过。
- 真实最小请求：成功返回一条作品。
- `reviewer` 已完成复测并允许提交。

### 4.8 提交信息

- commit：`2540078dd4c252c50a07e9ceefdc1af0bb63f26e`。
- commit 标题：`feat(m0): add minimal danbooru image request`。
- push：禁止执行。

## 5. 已完成任务 M0-003

### 5.1 任务名称

任务队列、取消、限流和应用关闭清理。

### 5.2 状态

已完成。

### 5.3 负责人

- 编码：`developer`。
- 测试与审查：`reviewer`。
- 暂存与本地提交：`intergrator`。

### 5.4 任务范围

- 建立可复用的任务状态模型，至少包含等待、执行、等待重试、成功、失败和取消。
- 建立有界任务队列，分别限制元数据请求和媒体请求的同时执行数量。
- 建立协作取消机制；等待任务立即取消，执行中的网络请求调用中止，取消后不得再发起重试。
- 建立请求间隔限制，连续请求必须遵守可以配置的最小时间间隔。
- 对 HTTP 429 和 503 建立有上限的重试机制，支持读取秒数形式和 HTTP 日期形式的 `Retry-After`。
- 将现有 Danbooru 单图请求接入取消、限流和重试机制，并在界面提供取消当前请求的操作。
- 应用退出时停止等待计时器、取消队列任务并中止活动网络请求。
- 使用本机模拟服务或可控制的测试替身验证行为；自动测试不得依赖 Danbooru 实际网络。
- 当前任务不实现分页、批量下载、任务持久化、暂停后继续、复杂查询或 Eagle 导入。

### 5.5 验收条件

- 同一任务的状态变化合法且可观察，终止状态不能重新进入执行状态。
- 元数据请求和媒体请求分别遵守配置的并发上限。
- 请求间隔限制能够延后过早的请求，测试不依赖真实长时间等待。
- HTTP 429、503 可以按照 `Retry-After` 等待后重试，超过次数上限后返回明确错误。
- 用户取消等待请求或活动请求后，不再开始新请求或重试。
- 关闭应用后，队列中没有等待任务、活动任务或重试计时器。
- 现有 M0-001、M0-002 功能与测试继续通过。

### 5.6 开发交接

- `developer` 已完成任务状态、分类并发队列、请求间隔、有限重试和关闭清理实现。
- 现有 Danbooru 单图请求已经接入任务队列，QML 已增加取消请求操作。
- 自检为 47 项 pytest、`compileall`、`headless-check` 和 QML 离屏退出清理通过。
- 本任务自检没有访问真实 Danbooru，当前等待 `reviewer` 独立复测。

### 5.7 首轮审查结果

- `reviewer` 结论为不通过。
- 任务进入执行状态时，状态监听器同步取消或关闭队列仍可能调用启动函数，并残留活动名额。
- 有效 `Retry-After` 被最大退避时间截短，可能早于服务端要求重新请求。
- 其余 47 项 pytest、`compileall`、`headless-check`、QML 离屏启动和常规退出清理通过；审查过程没有访问真实 Danbooru。

### 5.8 修正交接

- `developer` 已调整任务启动顺序，并在状态监听器返回后再次检查取消与关闭状态。
- 启动函数内部同步取消时，随后返回的活动句柄会立即中止；同步成功不会被误中止。
- 有效 `Retry-After` 已改为完整遵守服务端等待时间，本地上限只限制缺失或无效响应头时的退避。
- 新增相关回归测试后，53 项 pytest、`compileall`、`headless-check` 和 QML 离屏退出清理通过，当前等待 `reviewer` 复测。

### 5.9 第二轮审查结果

- `reviewer` 复测同步取消、同步关闭、启动函数内部取消和同步完成边界，首轮问题已经修正。
- 有效 `Retry-After` 完整遵守服务端等待时间，缺失或无效值使用有上限退避。
- 独立验证 53 项 pytest、`compileall`、全新数据目录 `headless-check`、QML 离屏启动和退出清理通过。
- 审查过程没有访问真实 Danbooru，结论为允许提交。

### 5.10 验证结果

- pytest：53 项通过。
- `compileall`：通过。
- 全新数据目录 `headless-check`：通过。
- QML 离屏启动和退出清理：通过。
- 验证过程没有发送真实网络请求。
- `reviewer` 已完成第二轮复测并允许提交。

### 5.11 提交信息

- commit：`7f8f7b0e3e0641b599f9ebc0b494254793fa53f5`。
- commit 标题：`feat(m0): add bounded task lifecycle`。
- push：禁止执行。

## 6. 已完成任务 M0-004

### 6.1 任务名称

Windows 壁纸技术验证。

### 6.2 状态

已完成。

### 6.3 负责人

- 编码：`developer`。
- 测试与审查：`reviewer`。
- 暂存与本地提交：`intergrator`。

### 6.4 任务范围

- 建立壁纸显示方式模型，包含填充、适应、拉伸、平铺和居中。
- 实现 Windows `WallpaperService`，使用 Unicode Win32 接口读取和设置桌面壁纸，并更新当前用户的壁纸显示方式。
- 设置操作使用绝对本地路径，检查文件存在、普通文件和允许的静态图片格式；GIF、WebM、MP4 和其他动态格式返回明确的不支持结果。
- 设置前保存当前壁纸路径和显示方式；支持在当前应用进程中恢复最近一次被替换的壁纸。
- 设置过程失败时保留可重试的上一张壁纸状态，并尽量恢复已经改动的显示方式。
- Windows 适配器使用真实服务；Linux 和其他平台继续返回明确的不支持或未实现状态，平台无关模块不得直接调用 Windows 接口。
- 增加只读 `--wallpaper-probe` 检查入口，读取当前壁纸信息并输出稳定 JSON，不修改桌面壁纸。
- 使用可注入的 Windows 后端测试设置、恢复、显示方式映射和错误处理；自动测试不得改变真实桌面壁纸。
- 当前任务不实现应用背景界面、图片转换、壁纸历史持久化、自动换壁纸、定时任务或 Linux 壁纸设置。

### 6.5 验收条件

- Windows 只读检查能够返回当前壁纸路径和显示方式，或返回包含错误代码与说明的失败结果。
- 五种显示方式能够转换为正确的 Windows 设置值。
- 不存在的路径、目录和不支持格式不会调用系统设置接口。
- 成功设置后可以恢复设置前的壁纸；重复恢复和没有历史时返回明确结果。
- 系统调用或注册表写入失败时返回明确错误，保存的恢复状态不会被错误覆盖。
- 模拟 Windows、Linux 和其他平台时均能导入并建立完整平台服务容器。
- 现有 53 项测试以及 M0-001 至 M0-003 功能继续通过。

### 6.6 开发交接

- `developer` 已完成 Windows Unicode 壁纸接口、五种显示方式、恢复操作、失败回退和跨平台延迟加载。
- 已增加只读 `--wallpaper-probe`；Windows 实机读取成功，当前显示方式为 `fit`。
- 自检为 108 项 pytest、`compileall`、`headless-check` 和 QML 离屏检查通过。
- 所有设置与注册表写入测试均使用替身，没有执行真实壁纸设置，当前等待 `reviewer` 独立复测。

### 6.7 首轮审查结果

- `reviewer` 结论为不通过。
- 首次设置失败且回退全部成功时仍会保存虚假恢复点，随后恢复会重复写入壁纸和注册表。
- 只有发生或可能发生部分改动且回退失败时才应保存失败前状态；已有成功设置形成的恢复点不得被覆盖。
- 其余 108 项 pytest、`compileall`、`headless-check`、QML 离屏检查和一次真实只读壁纸探测通过；没有执行真实设置或注册表写入。

### 6.8 修正交接

- `developer` 已改为仅在回退失败时保存首次失败前的恢复点。
- 回退全部成功后恢复操作返回 `no_restore_point`，并且不会再次调用系统或注册表后端。
- 已有成功设置形成的恢复点不会被后续失败覆盖。
- 壁纸专项 51 项测试、完整 108 项 pytest、`compileall`、`headless-check` 和 QML 离屏检查通过，当前等待 `reviewer` 复测。

### 6.9 第二轮审查结果

- `reviewer` 验证回退成功不再留下恢复点，回退失败仍可再次恢复，已有成功历史不会被覆盖。
- 壁纸专项 51 项测试通过；排除首轮已执行的真实只读探测后，107 项回归测试通过。
- `compileall`、全新目录 `headless-check` 和 QML 离屏检查通过。
- 本轮没有再次读取真实壁纸，没有执行真实设置或注册表写入，结论为允许提交。

### 6.10 验证结果

- 完整自检：108 项 pytest 通过。
- `reviewer` 第二轮壁纸专项测试：51 项通过。
- `reviewer` 第二轮回归测试：107 项通过；该轮排除了首轮已经执行的 Windows 真实只读 probe。
- `compileall`：通过。
- 全新数据目录 `headless-check`：通过。
- QML 离屏检查：通过。
- Windows 真实只读 probe 在首轮成功读取当前壁纸，显示方式为 `fit`。
- 验证过程没有执行真实壁纸 SET，也没有执行真实注册表写入。
- `reviewer` 已完成第二轮复测并允许提交。

### 6.11 提交信息

- commit：`5d466a9bd93f3f6a8d650e3e3f8183d9ac64f897`。
- commit 标题：`feat(m0): add windows wallpaper service`。
- push：禁止执行。

## 7. 当前任务 M0-005

### 7.1 任务名称

Eagle 单图导入技术验证。

### 7.2 状态

待开始。

## 8. 任务队列

- `SETUP-001`：项目协作与文档初始化，已完成。
- `M0-001`：应用空壳与跨平台目录结构，已完成。
- `M0-002`：Danbooru 最小请求与单图显示验证，已完成。
- `M0-003`：任务队列、取消、限流和应用关闭清理，已完成。
- `M0-004`：Windows 壁纸技术验证，已完成。
- `M0-005`：Eagle 单图导入技术验证，待开始。
- `M0-006`：Windows 与 Linux 构建检查，待开始。
- `M0-007`：M0 里程碑回归检查与提交，待开始。

## 9. 当前阻碍

- 暂无。

## 10. 当前开发环境

- Python：3.11.9。
- pytest：已安装。
- PySide6：6.12.0，已经安装在项目 `.venv` 中。
- Git 分支：`main`；项目初始化已建立本地提交，当前提交以 `git log` 为准。
- Git 远程：`origin` 指向项目 GitHub 仓库；`origin/main` 显示为已不存在。

## 11. 进度记录

### 2026-11-03

- 确认工作目录为 `E:\AIProjects\Danbooru__multi_search`。
- 确认项目已经建立 Git 仓库，当前分支为 `main`，尚无 commit。
- 建立开发阶段、Agent 协作和任务进度文档。
- 已复制需求文档，并创建 `developer`、`reviewer`、`intergrator` 三个子 Agent。
- `reviewer` 已完成文档审查并允许提交。
- `intergrator` 已创建首个本地 commit `889d5d33f92ed8995f81e77d1ceb8d66097596eb`，未执行 push。
- `intergrator` 已创建进度 commit `20e0783b873d5eab5e2c6a2cd75d3cbf005dd670`，未执行 push。
- 已启动 `M0-001`，由 `developer` 建立 PySide6 与 QML 应用空壳及跨平台目录结构。
- `developer` 已完成 M0-001 编码和自检：`compileall` 通过，9 项 pytest 通过，无界面入口及 Wheel 构建通过。
- `reviewer` 独立验证现有测试、设置、数据库、Wheel 和 QML 均通过，但发现平台服务接口及 Windows/Linux 空实现不完整，结论为不通过。
- 当前由 `developer` 补齐 `PlatformPaths`、`WallpaperService`、`MediaCapabilityService`、`EagleBridge`、`ExternalOpenService`、`CredentialStore`、`SystemInfoService` 接口及平台空实现。
- `developer` 已完成平台接口修正，自检为 14 项 pytest 通过、`compileall` 和无界面检查通过，当前等待 `reviewer` 复测。
- `reviewer` 复测 14 项 pytest、`compileall`、重复无界面初始化、平台服务完整性和空实现副作用，结论为允许提交。
- `intergrator` 已创建 M0-001 本地 commit `bbd37159bc83355dcf9fdf56ebc7ff0f26edf82a`，未执行 push。
- M0-001 已完成：14 项 pytest、`compileall` 和 `headless-check` 通过；Linux 尚未进行实机验证。
- 当前主要任务已切换为 `M0-002 Danbooru 最小请求与单图显示验证`，状态为待开始。
- `intergrator` 已创建 M0-002 本地 commit `2540078dd4c252c50a07e9ceefdc1af0bb63f26e`，未执行 push。
- M0-002 已完成：32 项 pytest、`compileall`、`headless-check` 和 QML 离屏加载通过；真实最小请求成功返回一条作品。
- 当前主要任务已切换为 `M0-003 任务队列、取消、限流和应用关闭清理`，状态为待开始。
- 已明确 M0-003 的任务状态、并发限制、请求间隔、重试、取消和关闭清理验收范围，状态改为进行中。
- `developer` 已完成 M0-003 编码和自检，47 项测试通过，当前等待 `reviewer` 独立复测。
- `reviewer` 首轮审查发现同步取消仍启动请求、有效 `Retry-After` 被截短两个问题，M0-003 进入修正。
- `developer` 已完成首轮问题修正，测试增至 53 项，M0-003 等待第二轮独立复测。
- `reviewer` 第二轮复测通过，M0-003 进入本地提交环节。
- `intergrator` 已创建 M0-003 本地 commit `7f8f7b0e3e0641b599f9ebc0b494254793fa53f5`，未执行 push。
- M0-003 已完成：53 项 pytest、`compileall`、全新数据目录 `headless-check`、QML 离屏启动和退出清理通过；验证过程没有发送真实网络请求。
- 当前主要任务已切换为 `M0-004 Windows 壁纸技术验证`，状态为待开始。
- 已明确 M0-004 的 Windows 原生服务、五种显示方式、恢复操作和只读检查范围，状态改为进行中。
- `developer` 已完成 M0-004 编码和自检，108 项测试通过，当前等待 `reviewer` 独立复测。
- `reviewer` 首轮审查发现失败后保存虚假恢复点的问题，M0-004 进入修正。
- `developer` 已完成虚假恢复点修正，M0-004 等待第二轮独立复测。
- `reviewer` 第二轮复测通过，M0-004 进入本地提交环节。
- `intergrator` 已创建 M0-004 本地 commit `5d466a9bd93f3f6a8d650e3e3f8183d9ac64f897`，未执行 push。
- M0-004 已完成：完整自检 108 项通过；`reviewer` 第二轮壁纸专项 51 项和回归测试 107 项通过，`compileall`、全新数据目录 `headless-check` 和 QML 离屏检查通过。
- Windows 真实只读 probe 在首轮成功读取显示方式 `fit`；没有执行真实壁纸 SET 或真实注册表写入。
- 当前主要任务已切换为 `M0-005 Eagle 单图导入技术验证`，状态为待开始。
