"""``smem`` command line interface (argparse only, no third-party deps)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .compiler import compile_project, consume_inbox, read_shadow_file
from .daemon import Daemon, run_sync
from .onboard import apply_init, apply_uninstall, inspect_agents
from .store import Store, resolve_db_path, resolve_home

PROG = "smem"


# --- output helpers ----------------------------------------------------------

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(t: str) -> str:
    return _c(t, "2")


def bold(t: str) -> str:
    return _c(t, "1")


def ok(t: str) -> str:
    return _c(t, "32")


def warn(t: str) -> str:
    return _c(t, "33")


def bad(t: str) -> str:
    return _c(t, "31")


def accent(t: str) -> str:
    return _c(t, "36")


def _kv(label: str, value: Any, width: int = 18) -> str:
    return f"  {dim(label.ljust(width))} {value}"


# --- shared ------------------------------------------------------------------

def _store(args) -> Store:
    return Store(args.db)


def _cwd(args) -> str:
    return os.path.abspath(getattr(args, "cwd", None) or os.getcwd())


def _do_sync(args, store: Store) -> Dict[str, Any]:
    return run_sync(
        store,
        cwd=_cwd(args),
        all_projects=bool(getattr(args, "all", False)),
        collect=True,
        sync_agents=bool(getattr(args, "inject_agents_md", False)),
        force_agents_sync=bool(getattr(args, "inject_agents_md", False)),
    )


def _print_sync_report(report: Dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    col = report.get("collected") or {}
    agents = col.get("agents") or {}
    if agents:
        print(bold("采集"))
        for name, r in agents.items():
            cnt = r.get("count", 0)
            extra = r.get("skipped") or r.get("error")
            line = _kv(name, f"{cnt} 条会话")
            if extra:
                line += dim(f"  ({extra})")
            print(line)
    if report.get("projects"):
        print(bold("编译"))
        for p in report["projects"]:
            print(
                _kv(
                    p.get("name") or "?",
                    f"{p['facts']} 记忆 · {p['sessions']} 会话 · "
                    f"{p['est_tokens']}/450 tokens"
                    + (dim("  · AGENTS.md 推迟(活跃中)") if p.get("agents_md_deferred") else ""),
                )
            )
    for err in report.get("errors") or []:
        print(bad("  ! " + err))


# --- commands ----------------------------------------------------------------

def cmd_init(args) -> int:
    store = _store(args)
    dry = not args.yes
    agents = inspect_agents(store, args.home)
    print(bold("Agent 探测"))
    for a in agents:
        if a["configured"]:
            state = ok("已接入")
        elif a["installed"]:
            state = warn("已安装 · 未接入")
        else:
            state = dim("未安装")
        print(_kv(a["label"], f"{state}  {dim(a['path'])}"))

    result = apply_init(
        dry_run=dry,
        create_missing=bool(args.create_missing),
        home=args.home,
        agents=args.agent or None,
    )
    print()
    print(bold("预览" if dry else "写入"))
    for r in result["results"]:
        if r.get("action", "").startswith("skipped"):
            print(_kv(r["agent"], dim(r["action"])))
            continue
        if r.get("changed") or "preview" in r:
            tag = ok(r["action"])
            print(_kv(r["agent"], f"{tag}  {dim(r['path'])}"))
            if r.get("backup"):
                print(_kv("", dim("backup → " + r["backup"])))
        else:
            print(_kv(r["agent"], dim("无变化")))
    if dry:
        print()
        print(dim("这是 dry-run。确认无误后运行： ") + accent(f"{PROG} init --yes"))
    else:
        print()
        print(ok("✓ 已接入。") + " 下一步： " + accent(f"{PROG} sync --all") + " 然后 " + accent(f"{PROG} start"))
    store.close()
    return 0


def cmd_uninstall(args) -> int:
    dry = not args.yes
    result = apply_uninstall(dry_run=dry, home=args.home, agents=args.agent or None)
    print(bold("预览" if dry else "卸载"))
    for r in result["results"]:
        action = r.get("action", "")
        if action.startswith("skipped"):
            print(_kv(r["agent"], dim(action)))
        elif r.get("changed") or "preview" in r:
            print(_kv(r["agent"], f"{ok(action)}  {dim(r['path'])}"))
            if r.get("backup"):
                print(_kv("", dim("backup → " + r["backup"])))
        else:
            print(_kv(r["agent"], dim("无变化")))
    if dry:
        print()
        print(dim("这是 dry-run。确认无误后运行： ") + accent(f"{PROG} uninstall --yes"))
    else:
        print()
        print(ok("✓ 已卸载，受控块已移除。"))
    return 0


def cmd_sync(args) -> int:
    store = _store(args)
    try:
        report = _do_sync(args, store)
        _print_sync_report(report, as_json=bool(args.json))
        return 0
    finally:
        store.close()


def cmd_write(args) -> int:
    store = _store(args)
    try:
        proj = store.ensure_project(_cwd(args))
        text = " ".join(args.text).strip()
        if not text:
            print(bad("错误：正文为空"), file=sys.stderr)
            return 2
        res = store.write_fact(
            content=text,
            project_id=proj["project_id"],
            scope=args.scope,
            kind=args.kind,
            evidence=args.evidence or "",
            confidence=args.confidence,
            source_agent=args.agent or "cli",
            ttl_days=args.ttl,
            supersedes_id=args.replace,
        )
        compiled = compile_project(store, proj, force_agents_sync=True, sync_agents=True)
        status = res.get("status")
        if status == "merged":
            print(ok("✓ 已合并") + f" 进相近记忆 {accent('#' + res['short_id'])}"
                  + dim(f" (jaccard={res.get('similarity')})"))
        elif status == "superseded":
            print(ok("✓ 已取代") + f" {accent('#' + res.get('superseded','')[:6])}"
                  f" → {accent('#' + res['short_id'])}")
        else:
            print(ok("✓ 已写入") + f" {accent('#' + res['short_id'])}")
        print(_kv("索引", f"{compiled['est_tokens']}/450 tokens · {compiled['facts']} 条 active"))
        return 0
    except KeyError as exc:
        print(bad("错误：" + str(exc)), file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_forget(args) -> int:
    store = _store(args)
    try:
        fact = store.get_fact(args.fact_id)
        res = store.forget_fact(args.fact_id, args.reason or "")
        if fact and fact.get("project_id"):
            proj = store.get_project(fact["project_id"])
            if proj:
                compile_project(store, proj, force_agents_sync=True)
        print(ok("✓ 已废弃") + f" {accent('#' + res['short_id'])}"
              + (dim(f"  原因：{args.reason}") if args.reason else ""))
        print(dim(f"  恢复： {PROG}  (Web 查看页点『恢复』，或 POST /api/facts/{res['short_id']}/restore)"))
        return 0
    except KeyError as exc:
        print(bad("错误：" + str(exc)), file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_status(args) -> int:
    store = _store(args)
    try:
        counts = store.counts()
        projects = store.list_projects()
        print(bold(f"shadow-memory {__version__}"))
        print(_kv("数据库", str(store.path)))
        print(_kv("数据目录", str(Path(store.path).parent)))
        print(_kv("FTS5", "可用" if getattr(store, "fts_enabled", False) else dim("不可用（降级 LIKE）")))
        print()
        print(bold("规模"))
        print(_kv("项目", counts["projects"]))
        print(_kv("活跃记忆", f"{counts['facts_active']} / {counts['facts_total']}"))
        print(_kv("会话档案", counts["sessions"]))
        print()
        print(bold("项目"))
        if not projects:
            print(dim("  （暂无项目，先 CD 到项目目录跑 smem sync）"))
        for p in projects:
            print(
                _kv(
                    p.get("name") or "?",
                    f"{p['fact_count']} 记忆 · {p['session_count']} 会话 · {dim(p['root'])}",
                )
            )
        print()
        print(bold("Agent 接入"))
        for a in inspect_agents(store, args.home):
            state = ok("已接入") if a["configured"] else (warn("未接入") if a["installed"] else dim("未安装"))
            print(_kv(a["label"], f"{state}  {dim(str(a['sessions_count']) + ' 会话')}"))
        if args.json:
            print()
            print(json.dumps({"counts": counts, "projects": projects}, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


def cmd_web(args) -> int:
    from .api import serve_forever

    store = _store(args)
    daemon = None
    if not args.no_daemon:
        daemon = Daemon(store, home=args.home).start()
    url = f"http://127.0.0.1:{args.port}/"
    print(bold(f"shadow-memory {__version__}"))
    print(_kv("查看页", accent(url)))
    print(_kv("数据库", str(store.path)))
    print(_kv("守护线程", ok("运行中") if daemon else dim("已禁用")))
    print(dim("  Ctrl+C 退出"))
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        serve_forever(store, "127.0.0.1", args.port, home=args.home, daemon=daemon)
    except KeyboardInterrupt:
        print(dim("\n已停止"))
    finally:
        if daemon:
            daemon.stop()
        store.close()
    return 0


def cmd_start(args) -> int:
    return cmd_web(args)


def cmd_inspect(args) -> int:
    store = _store(args)
    try:
        proj = store.ensure_project(_cwd(args))
        if args.file:
            data = read_shadow_file(Path(proj["root"]), args.file)
            if not data["exists"]:
                print(bad(f"文件不存在：{data['path']}"), file=sys.stderr)
                return 2
            sys.stdout.write(data["content"])
            return 0
        facts = store.search_facts(project_id=proj["project_id"], status="active", limit=500)
        sessions = store.list_sessions(proj["project_id"], limit=20)
        print(bold(proj["name"]) + dim("  " + proj["root"]))
        print(_kv("记忆", len(facts)))
        print(_kv("会话", len(sessions)))
        for f in facts[:10]:
            print(f"  {accent('#' + f['fact_id'][:6])} {dim('[' + f['kind'] + ']')} {f['content'][:90]}")
        return 0
    finally:
        store.close()


# --- parser ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a subcommand-level value never clobbers a global one
    common.add_argument(
        "--db", default=argparse.SUPPRESS,
        help="SQLite 数据库路径（默认 ~/.shadow-memory/memory.sqlite）",
    )
    common.add_argument(
        "--home", type=Path, default=argparse.SUPPRESS,
        help="Agent 配置根目录（默认用户主目录；测试用）",
    )

    p = argparse.ArgumentParser(
        prog=PROG,
        description="shadow-memory · 零依赖跨 Agent / 跨 Session 影子记忆服务",
        epilog=f"示例： {PROG} init --yes && {PROG} sync --all && {PROG} start --open",
    )
    p.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    # global copies: `smem --db X status` and `smem status --db X` both work
    p.add_argument(
        "--db", default=None,
        help="SQLite 数据库路径（默认 ~/.shadow-memory/memory.sqlite）",
    )
    p.add_argument(
        "--home", type=Path, default=None,
        help="Agent 配置根目录（默认用户主目录；测试用）",
    )
    sub = p.add_subparsers(dest="command", metavar="<command>")

    sp = sub.add_parser("init", parents=[common], help="接入本机 Agent（默认 dry-run）")
    sp.add_argument("--yes", "-y", action="store_true", help="实际写入（否则只预览）")
    sp.add_argument("--create-missing", action="store_true", help="为未安装的 Agent 创建配置目录")
    sp.add_argument("--agent", action="append", help="只处理指定 Agent（可重复）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("uninstall", parents=[common], help="移除受控块（默认 dry-run）")
    sp.add_argument("--yes", "-y", action="store_true", help="实际移除（否则只预览）")
    sp.add_argument("--agent", action="append", help="只处理指定 Agent（可重复）")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("sync", parents=[common], help="采集会话 + 消费 inbox + 重编译")
    sp.add_argument("--cwd", default=None, help="项目根目录（默认当前目录）")
    sp.add_argument("--all", action="store_true", help="所有已知项目")
    sp.add_argument("--inject-agents-md", action="store_true", help="强制同步 AGENTS.md（忽略活跃窗口推迟）")
    sp.add_argument("--json", action="store_true", help="JSON 输出")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("start", parents=[common], help="常驻：守护同步 + HTTP 查看页")
    sp.add_argument("--port", type=int, default=17820)
    sp.add_argument("--open", action="store_true", help="自动打开浏览器")
    sp.add_argument("--no-daemon", action="store_true", help="不启动后台同步线程")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("web", parents=[common], help="启动查看页并打开浏览器")
    sp.add_argument("--port", type=int, default=17820)
    sp.add_argument("--open", action="store_true", default=True)
    sp.add_argument("--no-daemon", action="store_true")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("write", parents=[common], help="写一条记忆")
    sp.add_argument("text", nargs="+", help="记忆正文")
    sp.add_argument("-k", "--kind", default="fact", help="类型：decision/constraint/preference/handoff/fact")
    sp.add_argument("--cwd", default=None, help="项目根目录（默认当前目录）")
    sp.add_argument("--scope", default="project", choices=["project", "global"])
    sp.add_argument("--replace", default=None, help="取代旧记忆（完整 ID 或 #短ID）")
    sp.add_argument("--evidence", default=None, help="依据")
    sp.add_argument("--confidence", type=float, default=0.8)
    sp.add_argument("--ttl", type=int, default=None, help="过期天数")
    sp.add_argument("--agent", default="cli", help="来源 Agent 名")
    sp.set_defaults(func=cmd_write)

    sp = sub.add_parser("forget", parents=[common], help="废弃一条记忆")
    sp.add_argument("fact_id")
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("restore", parents=[common], help="恢复一条被废弃的记忆")
    sp.add_argument("fact_id")
    sp.set_defaults(func=_cmd_restore)

    sp = sub.add_parser("status", parents=[common], help="查看服务状态与规模")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("inspect", parents=[common], help="查看当前项目编译结果")
    sp.add_argument("--cwd", default=None)
    sp.add_argument("--file", default=None, help="直接打印某个影子文件")
    sp.set_defaults(func=cmd_inspect)

    return p


def _cmd_restore(args) -> int:
    store = _store(args)
    try:
        res = store.restore_fact(args.fact_id)
        fact = store.get_fact(res["fact_id"])
        if fact and fact.get("project_id"):
            proj = store.get_project(fact["project_id"])
            if proj:
                compile_project(store, proj, force_agents_sync=True)
        print(ok("✓ 已恢复") + f" {accent('#' + res['short_id'])} (status={res['status']})")
        return 0
    except KeyError as exc:
        print(bad("错误：" + str(exc)), file=sys.stderr)
        return 2
    finally:
        store.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print(dim("\n已中断"))
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())