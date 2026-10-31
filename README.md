# Dualgent

面向真实 Web 与桌面 GUI 的自主交互智能体。用户选择标签页或窗口、描述目标，Dualgent 自己管理任务、观察环境、规划动作，并在每次操作后重新观察和验证结果。

当前为 **0.1.0，第一阶段可运行版本**。独立运行，不依赖 BrowserAttach、Nosis、agent-browser 或 MCP 服务。

```text
工作台 → 任务服务 → InteractAgent → 统一交互契约
                     ↓               ├─ 浏览器扩展 → Chrome / Edge 已有标签页
                SQLite 执行记录       └─ Windows 驱动 → UI Automation / 原生输入
                     ↑                        ↓
                     └──────── 新观察与结果验证 ┘
```

## 开始使用

需要 Python 3.11+。Windows 桌面操作需要可交互的 Windows 登录会话。浏览器支持 Chrome / Edge 125+；扩展不要求开启远程调试端口。

```powershell
git clone https://github.com/EvannZhongg/Dualgent.git
cd Dualgent
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[windows]"
Copy-Item .env.example .env
```

在 `.env` 中填入 `DEEPSEEK_API_KEY`，然后启动：

```powershell
.venv\Scripts\dualgent --open
```

工作台默认位于 <http://127.0.0.1:8765>。`--open` 使用仅存在于 URL fragment 的本机连接码登录，页面加载后移除 fragment。直接访问地址时，可以输入 `.dualgent/connection-token` 文件中的连接码。

macOS / Linux 安装基础依赖即可运行工作台和浏览器能力：`python -m pip install -e .`。当前尚无这两个平台的桌面驱动。

### 连接浏览器

1. 点击工作台左下角的「连接浏览器」，查看扩展目录并复制连接码。
2. 在 `chrome://extensions` 或 `edge://extensions` 开启开发者模式，选择「加载已解压的扩展程序」，加载该目录。
3. 打开 Dualgent 扩展设置，粘贴连接码、填写服务端口，保存并连接。
4. 回到工作台刷新环境，选择任务可以操作的标签页或桌面窗口，输入目标后开始。

从源码安装时，扩展目录为 `src/dualgent/extension`；从 wheel 安装时，工作台会显示安装后的实际目录。它是 Dualgent 自有的扩展，不使用 BrowserAttach 扩展。

一个任务最多选择 4 个环境，可以跨 Web 与桌面搬运信息。Web 任务限制在所选标签页的初始 origin；跳到其他站点时停止操作，需要重新选择环境创建任务。普通 Web 操作不激活标签或移动系统鼠标。Windows 优先使用 UIA 模式，原生点击或按键确实需要时才请求窗口焦点。

### 任务与验证

- 每次派发前，先提交决策与执行意图；回执区分 `dispatched`、`rejected` 和 `unknown`。
- 输入派发后重新读取环境，核对文本、标题、URL 或元素的精确名称和值。回执本身不能使任务完成。
- 最终完成必须再次通过验证，且检查覆盖任务实际操作过的环境。验证结果表达可观察界面事实，不代替服务端业务审计。
- 动作超时、连接中断或结果未知时进入「等待你的回复」，不自动重放动作。用户检查实际结果并补充说明后，才重新观察和决策。
- 默认每轮最多 24 个决策；继续任务获得新一轮预算，旧记录完整保留。第三次相同且环境未变化的操作会在派发前停止。
- 「停止任务」阻止后续输入；已经派发的动作不会回滚。正在等待的模型调用或驱动请求会在返回或超时后到达取消检查点。
- 服务重启时，执行中的任务进入等待状态；排队但尚未执行的任务会继续排队。数据目录有单写者锁。用户继续任务时可以更新同一环境的标题或同源 URL，不能自动换到新窗口、新浏览器会话或其他站点。

## 配置与文件

手工配置仅需 `.env`：

```dotenv
DEEPSEEK_API_KEY=...
```

可选 `DUALGENT_MODEL` 和 `DUALGENT_API_URL` 用于另一个兼容接口；默认分别为 `deepseek-flash` 和 `https://api.deepseek.com`。服务位置用启动参数 `--port`、`--data-dir` 调整。

`.dualgent/` 保存连接码、SQLite 任务与事件。任务选定的页面或窗口内容会发送给配置的模型，并保存在本机执行记录中。API Key 只在服务端读取，不进入工作台、扩展或任务记录。`.env`、参考项目、本机数据和验证浏览器 profile 均已被 Git 忽略。

## 验证与开发

```powershell
.venv\Scripts\python -m pip install -e ".[windows,test]"
.venv\Scripts\python -m pytest -q
node --test tests/extension.test.mjs
.venv\Scripts\ruff check src tests scripts
```

真实模型验证会计费，只操作脚本创建的本地测试页面和一次性 Windows 测试应用：

```powershell
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python scripts/validate_stage.py --desktop
```

只验证 Web 时省略 `--desktop`；已有 Playwright Chromium 可以使用 `--chromium <chrome.exe路径>`。Windows 测试应用通过系统 .NET Framework C# 编译器构建。脚本启动隔离 profile、加载独立扩展、执行真实模型任务，并独立检查浏览器存储和桌面输出文件；报告、截图、完整执行记录保存到 `.dualgent/validation-*/`。Playwright 只负责测试环境准备与只读断言，任务输入由生产驱动执行。

接口定义见 [contracts.py](src/dualgent/contracts.py)。架构、参考项目分析及 macOS 接入方式见 [架构设计](docs/architecture.md)，本阶段实际验证结果见 [阶段验证](docs/stage-1.md)。

## 当前边界

已支持语义树上的点击、填值、有限按键、滚动以及 Web 同源导航；已验证浏览器多步任务和 Web → Windows 跨环境任务。

本阶段尚不支持截图视觉定位、画布交互、跨 frame 元素操作、上传、拖拽、新建标签页、macOS 桌面或无人值守敏感操作。Windows 填值要求控件提供可写的 UIA ValuePattern；UIA 缺失的应用不能自动切换到坐标盲点。需要切换到模态窗口时必须显式选择该窗口。

敏感操作的询问由模型指令驱动，尚无独立风险识别器和冻结动作审批机制，因此此版本适合在可审查、可恢复的任务中验证交互闭环。
