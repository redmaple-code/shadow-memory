# shadow-memory

> 让 Claude Code、Codex、ZCode、Antigravity 共用一份**影子记忆**——跨 Agent、跨 Session，纯本机、零依赖。

你在 A 工具里拍板的方案，B 工具下一轮就该知道；昨天聊定的约束，今天不该再问一遍。
`shadow-memory` 把散落在各 Agent 会话里的结论、约束、交接，压缩成一份 **≤450 tokens 的索引**，
每个 Agent 开工时只读这几百个 token 就能接上现场。

---

## 为什么值得用

| | |
|---|---|
| **零第三方依赖** | 只用 Python 3.10+ 标准库（`sqlite3` / `http.server` / `re` / `argparse`…）。`pip install -e .` 或 `python -m shadow_memory` 秒起，Windows / macOS / Linux 通用。 |
| **不花 Token 就能采集** | Claude Code / Codex / ZCode 的会话由正则 + 结构化解析提取（标题、末轮结论、涉及文件、未完事项），**不调用任何大模型**。 |
| **写入即脱敏** | 所有记忆与归档流水落库前先过 `redact.py`：`sk-` / `ghp_` / `AKIA` / Bearer / JWT / 私钥块 / 手机号 / 邮箱 / 身份证 一律打码。 |
| **本机封闭** | HTTP 只绑 `127.0.0.1:17820`，无外链 CDN、无遥测、断网可用。 |
| **缓存安全** | 会话活跃（180s 内）时**不**改 `AGENTS.md`，避免打断宿主的 prompt 缓存；内容没变绝不碰 `mtime`。 |
| **可回滚** | 接入前自动 `.bak.<时间戳>` 备份；`smem uninstall --yes` 无痕移除受控块。 |

---

## 3 分钟上手

### 1. 安装

```bash
git clone https://github.com/redmaple-code/shadow-memory.git
cd shadow-memory
pip install -e .          # 得到 smem 命令；也可不装，用 python -m shadow_memory
```

验证（不需要任何第三方库）：

```bash
smem --version            # smem 0.1.0
smem status
```

### 2. 接入本机 Agent（先看预览，再落盘）

```bash
smem init                 # dry-run：只显示将要改哪些文件
smem init --yes           # 实际写入（每个文件先备份 .bak.<时间戳>）
```

会探测 4 个位置，**只改已存在的配置**（不存在就不创建，除非加 `--create-missing`）：

| Agent | 配置文件 |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` |
| ZCode | `~/.zcode/AGENTS.md` |
| Antigravity / Gemini | `~/.gemini/GEMINI.md` |

写入的是一段**受控块**（`<!-- shadow-memory:start -->` … `<!-- shadow-memory:end -->`，4 行协作约定），
重复执行不会叠加，卸载时只删这一段。

### 3. 首次同步 + 启动

```bash
cd /path/to/your/project
smem sync --all           # 采集本机会话 + 消费 inbox + 编译 .agents/
smem start --open         # 常驻：后台同步线程 + Web 查看页（自动开浏览器）
```

`smem start` 之后：

- 每 **5 秒**消费一次 `项目/.agents/inbox.jsonl`（写记忆几乎实时生效）；
- 每 **60 秒**增量采集会话并重编译。

只想看看界面、不想常驻：`smem web`（同样会开浏览器；两者默认端口都是 `17820`，可用 `--port` 改）。

### 4. 开始记东西

```bash
smem write -k decision "订单金额一律用整数分存储，不用浮点"
smem write -k constraint "禁止在日志里打印用户手机号"
smem write -k handoff  "导出功能还差 CSV 分支没测"
```

改口（取代旧结论，旧条自动置为 `superseded` 并保留完整版本链）：

```bash
smem write -k decision --replace #63bd40 "订单金额改用 Decimal(18,2)"
```

写错了：

```bash
smem forget 63bd40 --reason "记错项目了"     # 支持 6 位短 ID 或完整 ID
smem restore 63bd40                         # 误删可恢复（Web 页也有按钮）
```

---

## 命令一览

| 命令 | 说明 |
|---|---|
| `smem init [--yes] [--create-missing] [--agent codex]` | 接入 Agent，默认 dry-run |
| `smem uninstall [--yes]` | 移除受控块，默认 dry-run |
| `smem sync [--cwd .] [--all] [--inject-agents-md] [--json]` | 采集 + 消费 inbox + 重编译 |
| `smem start [--port 17820] [--open] [--no-daemon]` | 常驻守护 + HTTP 查看页 |
| `smem web [--port 17820]` | 同上，并自动打开浏览器 |
| `smem write -k <kind> <text> [--cwd .] [--replace #id] [--evidence ...] [--ttl 天]` | 写一条记忆 |
| `smem forget <fact_id> [--reason ...]` | 废弃一条记忆 |
| `smem restore <fact_id>` | 恢复被废弃的记忆 |
| `smem status [--json]` | 数据库路径、规模、项目、Agent 接入状态 |
| `smem inspect [--file MEMORY.md]` | 查看当前项目的编译结果 |

全局参数 `--db <path>` / `--home <dir>` 可以写在子命令前，也可以写在子命令后。
数据默认落在 `~/.shadow-memory/memory.sqlite`，用环境变量 `SHADOW_MEMORY_HOME` 可整体改目录。

`-k/--kind` 常用值：`decision`（拍板）、`constraint`（红线）、`preference`（偏好）、
`handoff`/`todo`（交接）、`fact`（普通事实）。索引里按这个优先级排序。

---

## 4 Tab Web 查看页

`http://127.0.0.1:17820/` — 单文件零构建，深色/浅色可切换，三栏布局：

1. **记忆账本** — 搜索（中英文 FTS5）+ 类型/状态筛选 + 「+ 手动记一条」；紧凑表格列出
   短ID / 类型 / 正文 / 来源 Agent / 更新时间 / 状态点。
2. **跨会话现场** — 顶部实时并发脉冲条（`[RUNNING]` 3 分钟内 / `[RECENT]`）+ 跨 Agent 会话时间线，
   涉及文件用胶囊展示。
3. **影子文件预览** — `MEMORY.md`（带 `xxx / 450 tokens` 实时进度条）、`decisions.md`、
   `LIVE_PULSE.md`、`inbox.processed.jsonl` 切换查看；「立即重编译」「消费 inbox」按钮。
4. **接入与状态** — 4 大 Agent 的路径 / 状态 / 已采集会话数，命令复制框，隐私说明。

右侧**非模态检查器**：点记忆行看完整正文、依据、来源，以及**可视化版本演进链**
（旧 `#id` → 新 `#id`），可「废弃 / 恢复 / 改口取代」；点会话行看摘要、末轮结论、未完事项与文件清单。

### HTTP API（同一端口）

| 方法 | 路径 |
|---|---|
| GET | `/` · `/api/status` · `/api/projects` · `/api/agents` |
| GET | `/api/facts?project_id=&kind=&status=&q=` · `/api/facts/<id>/chain` |
| GET | `/api/sessions?project_id=` · `/api/shadow?project_id=&file=MEMORY.md` |
| POST | `/api/facts`（新增/取代） · `/api/facts/<id>/forget` · `/api/facts/<id>/restore` |
| POST | `/api/sync` · `/api/compile` · `/api/inbox/consume` |

写操作完成后会自动重编译该项目，索引不会滞后。

---

## `项目/.agents/` 结构

```
项目/
├── AGENTS.md                    # ← 只注入这一小段受控块（缓存安全，活跃期不更新）
└── .agents/                     # git 自动忽略（写入 .git/info/exclude，无 git 则本地 .gitignore）
    ├── MEMORY.md                # 450 token 索引：Top3 红线 + 最新交接 + 最近会话
    ├── decisions.md             # 全量 active 记忆，含完整 fact_id、依据、来源
    ├── LIVE_PULSE.md            # 30 分钟内活跃会话（真实总数，展示前 10，RUNNING/RECENT）
    ├── sessions/
    │   ├── s_<8位hex>.md        # 单次跨 Agent 会话档案卡（最近 15 次）
    │   └── latest.md            # 最近 15 次会话一览
    ├── inbox.jsonl              # ← 你/Agent 往这里丢一行 JSON，5 秒内被消费
    └── inbox.processed.jsonl    # 已消费归档（脱敏，保留最近 200 行）
```

`MEMORY.md` 的预算分配是**整块取舍**而非截字：优先级为 头部 → Top 3 → 最新交接 →
最近 3~5 条会话标题 → 现场脉冲。放不下的整块丢弃，不会出现半句话。

### 往 inbox 丢一条

```jsonc
{"kind": "decision", "text": "CI 只用 ubuntu-latest，不跑 Windows 矩阵"}
{"kind": "decision", "text": "改用 pnpm", "replace": "#63bd40"}
{"content": "约束也支持 content 字段", "scope": "project", "evidence": "会议纪要"}
```

`replace` / `supersedes_id` 支持 `#6位短ID` 或完整 ID。

---

## 架构

```
Claude Code ─┐
Codex CLI  ──┼─→ collectors/（零 Token 解析）
ZCode DB   ──┘        │
                      ▼
              store.py  SQLite WAL + FTS5(CJK bigram)
               │  ▲            │
   inbox.jsonl ┘  └─ 版本链 / 去重(Jaccard≥0.80) / TTL 过期
                      ▼
              compiler.py  450 token 软预算 + 6 位短 ID
                      │
        ┌─────────────┼──────────────┐
        ▼             ▼              ▼
  .agents/MEMORY.md  AGENTS.md   api.py (127.0.0.1:17820)
  decisions.md       受控块       └→ web_ui.py 单页 4 Tab
  LIVE_PULSE.md
```

关键设计：

- **去重**：字符 bigram Jaccard ≥ 0.80 自动合并（同项目 + 同 scope + 同 kind），保留更完整的表述并合并依据。
- **取代**：`supersedes_id` 双向指针（`superseded_by`），`get_fact_chain()` 按指针拓扑行走，不依赖时间戳排序。
- **中文检索**：FTS5 `unicode61` 会把整串汉字当一个词，因此索引与查询都做 bigram 展开（`影子记忆` → `影子 子记 记忆`），实现子串命中。
- **缓存安全**：180s 内有活跃会话就推迟 `AGENTS.md` 更新（`agents_md_deferred=True`）；内容相同则不写文件。

---

## 真实盲测基准

使用真实独立 Agent 会话（零路径提示、零 Session ID 提示）跑完 **10 组端到端盲测**（涵盖跨会话综合总结、跨 Agent 接力、20 会话抗噪深埋检索、3 次连续改口版本冲突、未知项拒答防幻觉、同名双模块防串味、零预填全链路接力、并发双窗口缓存保护）：

| 评估维度 | 实测结果 |
|---|---|
| **无感跨 Session / 跨 Agent 召回率** | **100%（10 / 10 组全通过）** |
| **连续多次改口冲突消解准确率** | **100%（零新旧混答）** |
| **未知内容拒答 / 防幻觉率** | **100% 拒答（0% 幻觉）** |
| **同名模块防串味率** | **0% 串味** |
| **并发活跃期 Prompt Cache 保护** | **100% 生效（`agents_md_deferred=True`，静态头 0 字节变动）** |
| **常驻索引体积** | **≤ 450 tokens**（静态前缀高缓存命中，几乎零额外 Token 税） |

---

## 测试

```bash
python -m pytest -v       # 5 个测试文件共 198 个用例，全部使用 tmp_path 隔离，绝不碰真实 ~/.zcode 等目录
```

---

## 卸载

```bash
smem uninstall --yes      # 先备份，再移除受控块；只含受控块的文件会被删掉
```

数据删不删由你决定：`rm -r ~/.shadow-memory`（Windows：删除 `%USERPROFILE%\.shadow-memory`）。
每个项目里的 `.agents/` 也可以直接删。

---

## License

MIT