# Godot CVar 控制台系统 — 会话交接

给下一个 agent 用。仓库 `F:\godot`，Godot 4.8 beta 自编译（`version.py` 一带，二进制名 `bin\godot.windows.editor.x86_64.exe`）。对话日期 2026-11-02。用户称呼波哥，回复用简体中文。

这份文档只覆盖 **本会话做的 CVar / 控制台**。仓库里还有未提交的 RVT/SVT，不是这次任务，别混进 CVar 的改动说明里。

## 用户要什么

1. 问：Godot 有没有 Unreal 那种 CVar 命令行。结论：原版没有。
2. 仿 Unreal CVar，在引擎里做一套运行时可改的命令行系统，并自测。
3. 编辑器里也要能打开，否则不实用。
4. 开关键：数字 1 左边那个键，按一下就开，不要额外按 Shift。`` ` `` 和 `~` 都算这个键。
5. 按一下显示，再按一下隐藏。第二次不能把这个键打进输入框。

原版 Godot 已有、但不是这套东西的：

- `Ctrl+Shift+P`：编辑器命令面板（打开场景、跑命令），不是运行时变量。
- `F1`：Search Help。编辑器里绝不能抢走。
- 调试器 / `-d`：断点，提示符 `debug>`，没有 CVar 注册表。
- `main/main.cpp` 里写死的 `--rendering-method`、`--max-fps` 等。不认识的参数不会变成变量。
- `project.godot` + `override.cfg`：多数设置只在启动时读。

`misc/rvt_demo/cvars.gd` 是这个仓库里后加的 **demo Autoload**，F1 控制台，只服务 RVT 调试命令（`r.VT.RVT.*`）。没有接到引擎单例 `CVars`。两边的 `--cvar-exec` 写法不同，目前不冲突。不要把它当成引擎原装功能，也不要未经要求就删掉或强行合并。

## 已落地的行为

脚本单例名 **`CVars`**。类型只支持 int / float / bool / String。名字不区分大小写。

优先级从低到高（低的盖不掉高的；同一优先级后写的赢；同值再写一次也会把优先级抬高）：

`SET_BY_CONSTRUCTOR` < `SET_BY_PROJECT_SETTING` < `SET_BY_CONFIG_FILE` < `SET_BY_COMMAND_LINE` < `SET_BY_CODE` < `SET_BY_CONSOLE`

- `FLAG_READ_ONLY`、`FLAG_CHEAT`：挡住配置文件、命令行、控制台。`CVars.set_value()`（code 优先级）仍可写。
- cheat 默认：`DEBUG_ENABLED` 构建允许控制台写 cheat；发布构建不允许。开关是 C++ `set_cheat_commands_allowed()`，没有绑到脚本。
- 命令语法：`name`、`name value`、`name=value`、`help [filter]`、`dump [filter]`、`reset name`。`;` 分隔多条。引号里的 `;` 不分割。`#` 和 `;` 开头、以及 `[Section]` 行忽略。
- 未知名字：命令行和配置文件先挂起，`register_variable` 时再套用。控制台里当场打未知名字是 error，不挂起。
- 内置变量：
  - `t.TimeScale`：立刻 `Engine.set_time_scale`
  - `t.MaxFPS`：立刻 `Engine.set_max_fps`（≤0 当无限）
  - `con.AutoConsole`：调试构建默认 true，发布默认 false。只影响 **游戏进程** 是否自动挂控制台。编辑器是另外挂的。
  - `con.ConfigPollMs`：默认 500。`--cvar-file` 的重读间隔，0 表示每帧。

启动参数（必须在 `--` **前面**，是引擎参数不是 user args）：

```text
godot --cvar t.TimeScale=0.25 --cvar t.MaxFPS=17 --cvar-exec "dump t." --cvar-file user://cvars.cfg
```

也接受单 token：`--cvar=name=value`、`--cvar-exec=...`、`--cvar-file=path`。

`Main::setup2()` 末尾顺序：先把 Engine 里已经生效的 time scale / max fps 种进 CVar（不回调），再读配置文件，再执行命令行。所以 `--time-scale` 高于配置文件；`--cvar` 和它同级，后写的赢。`Main::iteration()` 里按间隔重读 `--cvar-file`。

调试器：游戏进程注册了 capture `cvar` / 消息 `exec`。编辑器在调试会话连着时，把同一行再 `send_message("cvar:exec", [line])`。本地不认识的名字，控制台额外显示 `sent to running game`，游戏输出在编辑器 Output。本地认识的，两边都会改。

### 控制台窗口

类 `CVarConsole`（`GDREGISTER_INTERNAL_CLASS`，不是脚本 API）。

| 场景 | 怎么开 | 怎么关 |
|---|---|---|
| 游戏（非编辑器、非 headless、`con.AutoConsole`） | F1，或数字 1 左边那个键（`` ` `` / `~`） | 再按一次同一个键，或 Esc |
| 编辑器 | 没在文本框里打字时按那个键；菜单 Editor → Toggle CVar Console；命令面板同名。快捷键注册名 `editor/toggle_cvar_console`，默认 `Key::QUOTELEFT` | 再按一次。开着时这一下在输入框拿到字符之前就被吃掉 |

编辑器 **不抢 F1**。文本框已经有焦点、且控制台是关的时，这个键仍打进文本框（脚本里要打反引号）。控制台开着时，这个键只负责关掉，不打进输入框。

Windows 上这个键的键码是 `Key::QUOTELEFT`（96）。按住 Shift 时字符是 `~`，键码常常仍是 `QUOTELEFT`。判定函数是 `CVarConsole::is_tilde_toggle()`：`QUOTELEFT`、`ASCIITILDE`、unicode `` ` `` 或 `~` 都算。Ctrl/Alt/Meta 不算，避免吃掉 `Ctrl+`` `（那是 `ui_swap_input_direction`）。

### 已踩过的坑（不要再踩）

第二次按键关不掉，字符进了输入框。日志实锤（编辑器 stderr / 当时的 print）：

```text
input open=0 pressed=1 keycode=96 unicode=96 phys=96
editor shortcut TOGGLE open_before=0
input open=1 pressed=1 keycode=0 unicode=183 phys=4194313
```

第一下是裸键码 96，快捷键打开窗口并 `grab_focus()`。`LineEdit` 一编辑就会 `window_set_ime_active(true)`。中文输入法打开后，第二下不再是 keycode 96，而是输入法交出来的字符（实测 unicode 183，keycode 0）。快捷键对不上，`LineEdit::unhandled_key_input` 把它插进文本。

修法：控制台处于打开状态时 `_suppress_ime()`，并且 `NOTIFICATION_PROCESS` 里持续关掉 IME（`LineEdit` 会在焦点/光标变化时再打开）。另外 `LineEdit` 的 `gui_input` 信号比它自己插入字符更早，命中切换键就 `accept_event()` 并收起。

直接调用 `console->input()` 的单测覆盖不到这条路径。真窗口里复现过：第一张图底部有输入框，第二张图窗口已收起。

`SetForegroundWindow` 经常抢不到焦点，按键会打到别的窗口。自测要用 `AttachThreadInput` 再 `SetForegroundWindow`。Cursor 的 computer-use 在这台机器上起不来（`sidecar_unavailable`），不要依赖它。

链接 `bin\godot.windows.editor.x86_64.exe` 时如果编辑器还开着，会 `拒绝访问`。先关掉 `godot.windows.editor.x86_64` 再链。`.console.exe` 只是包装进程，真正的窗口在不带 `.console` 的那个进程上。

`vformat` 的 `%d` 不能喂 bool，会报 `a number is required`。日志用 `itos()`。

`RichTextLabel::set_offset` 盖住了 `Control::set_offset`，布局要用 `log_label->Control::set_offset(...)`。

`EngineDebugger::capture_parse` 不是静态方法。测试走 `CVars::handle_debugger_message()`。

## 文件

新增：

- `core/config/cvars.h`
- `core/config/cvars.cpp`
- `scene/debugger/cvar_console.h`
- `scene/debugger/cvar_console.cpp`
- `doc/classes/CVars.xml`
- `tests/core/config/test_cvars.cpp`
- `.planning/2026-11-02-godot-cvar/`（task_plan / findings / progress）

改过：

- `core/register_core_types.cpp`：创建单例，`register_early_core_singletons()` 里注册名字 `CVars`，退出时删。
- `main/main.cpp`：帮助文本、解析 `--cvar*`、`setup2` 末尾 `sync_engine_builtins` + `apply_startup`、`iteration` 里 `poll_config_file`。`--time-scale` 会把 `time_scale_cmdline` 设上。
- `scene/main/scene_tree.cpp`：`initialize()` 里，仅当 main loop 就是这棵树、不是 editor hint、不是 headless、`con.AutoConsole` 为真，才 `add_child` 一个游戏控制台。单元测试自己 `memnew(SceneTree)` 不会被塞控制台。
- `scene/register_scene_types.cpp`：`GDREGISTER_INTERNAL_CLASS(CVarConsole)`。
- `editor/editor_node.h` / `editor/editor_node.cpp`：编辑器控制台、快捷键、菜单项 `EDITOR_TOGGLE_CVAR_CONSOLE`、发给游戏的 hook `_editor_cvar_submitted`。
- `doc/classes/@GlobalScope.xml`：补了 `CVars` 单例说明。

`doc/classes/RenderingServer.xml` 在跑 `--doctool` 时相对 HEAD 多了一批 RVT 方法插入。那是仓库里已有的 RVT 工作，不是 CVar 功能本身。不要为了 CVar 去重写它。

## 编译和验证

MSVC 环境（这台机器是 VS 2022 Preview）：

```text
call "C:\Program Files\Microsoft Visual Studio\2022\Preview\VC\Auxiliary\Build\vcvars64.bat"
set MSYSTEM=
cd /d F:\godot
scons platform=windows target=editor arch=x86_64 tests=yes -j32
```

```text
bin\godot.windows.editor.x86_64.exe --headless --test --test-case=*CVars*
```

最后一次结果：**3 cases，162 assertions，0 failed**。用例：

- `[CVars] priority, parsing, file, and engine builtins`
- `[SceneTree][CVars] in-game console toggles and submits`
- `[SceneTree][CVars] editor console does not steal F1`（含第二下关闭，以及 `push_input` 后输入框文本仍为空）

另外跑过：

- `--help` 里有 `--cvar` / `--cvar-exec` / `--cvar-file`
- `--headless --quit --cvar t.MaxFPS=17 --cvar t.TimeScale=0.25` 打出这两行赋值
- 真窗口：第一下打开，第二下收起（截图在 `E:\AiTemp\cvar_ime_press1.png` 和 `cvar_ime_press2.png`）

打开编辑器给波哥试：

```text
F:\godot\bin\godot.windows.editor.x86_64.exe --path F:\godot\misc\rvt_demo --editor
```

点视口空白处再按 `` ` ``。光标如果还在脚本或属性过滤框里，这一下会打成字符，这是故意的。

## 给下一个 agent 的约束

- 改代码前先看 `.planning/2026-11-02-godot-cvar/`。波哥的规则是改源码前走 planning-with-files，计划写在仓库 `.planning/`，不要写到 skill 安装目录。
- 回复波哥用简体中文，直接，叫波哥。
- 不要猜没读到的数字。键码 96、unicode 183、断言条数都是这次实测的。
- 引擎注释和文档用英文。
- 未明确要求不要提交、不要推远程。
- `cvars.gd` 的 RVT 命令没有注册进 `CVars`。要在引擎控制台里打 `r.VT.RVT.DebugView`，得另做注册，这次没做。
- 控制台打开时 IME 是关的。调试命令用英文和数字即可。不要为了“能打中文”把 IME 重新打开，除非同时解决第二下被输入法吃掉的问题。
- 临时调试用的 `print_line` / `E:\AiTemp\cvar_keylog.txt` 写文件已经从代码里拿掉，不要再加回去。
