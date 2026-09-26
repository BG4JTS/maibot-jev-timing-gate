"""plugin.py 的离线集成测试（不需要 MaiBot SDK、不联网）。

直接跑：python tests/test_plugin_gate.py

两个关键技巧：
1. **SDK 桩**：`maibot_sdk` 未安装，必须在 import 插件代码**之前**把桩塞进
   `sys.modules`。桩的 `PluginConfigBase` 用**真实 pydantic**（``extra: allow``），
   这样 `config.py` 不用改任何一行就能通过校验，`GateSettings(...)` 是真模型。
2. **合成包**：仓库目录名 ``maibot-jev-timing-gate`` 含连字符，不是合法 Python
   标识符，无法直接 ``import plugin``。这里用一个合成的包模块 ``jev_gate_pkg``
   （``__path__ = [REPO_ROOT]``）把仓库根目录挂成包路径，再用
   ``spec_from_file_location("jev_gate_pkg.plugin", ...)`` 加载 plugin.py，
   使 plugin.py 内部的相对导入 ``from . import gate_core as core`` 与
   ``from .config import GateSettings`` 都能正常解析（gate_core.py / config.py
   会作为 ``jev_gate_pkg.gate_core`` / ``jev_gate_pkg.config`` 被自动导入）。

网络护栏：全程替换联网入口——``urllib.request.urlopen``（插件唯一的网络面）、
``socket.create_connection``（任何 HTTP 客户端建连入口）与 ``socket.getaddrinfo``
（DNS 解析入口）——为抛 ``AssertionError`` 的函数：任何联网尝试都会立刻失败，
而不是挂起。刻意**不**替换 ``socket.socket`` 本身：Windows 上 asyncio 的事件循环
自建 self-pipe 需要 ``socket.socketpair()``，拦掉它会让测试根本起不了循环。
"""

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import types
import urllib.request

import pydantic

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 1. 先把 maibot_sdk 桩塞进 sys.modules（必须在 import 插件代码之前）
# ---------------------------------------------------------------------------

_sdk = types.ModuleType("maibot_sdk")


def _hook_handler(*args, **kwargs):
    """HookHandler 桩：工厂，返回一个「原样返回函数」的装饰器。"""

    def deco(fn):
        return fn
    return deco


_sdk.HookHandler = _hook_handler


class _MaiBotPlugin:
    """MaiBotPlugin 桩：普通类即可（插件子类不需要 SDK 的元类/初始化）。"""


_sdk.MaiBotPlugin = _MaiBotPlugin


class _PluginConfigBase(pydantic.BaseModel):
    """PluginConfigBase 桩：真实 pydantic 底座 + extra allow，config.py 原样可用。"""

    model_config = {"extra": "allow"}


_sdk.PluginConfigBase = _PluginConfigBase
_sdk.Field = pydantic.Field

_types = types.ModuleType("maibot_sdk.types")


class _HookMode:
    BLOCKING = "BLOCKING"


class _HookOrder:
    EARLY = "EARLY"


_types.HookMode = _HookMode
_types.HookOrder = _HookOrder

_sdk.types = _types
sys.modules["maibot_sdk"] = _sdk
sys.modules["maibot_sdk.types"] = _types

# ---------------------------------------------------------------------------
# 2. 合成包：把仓库根挂成包路径，加载 plugin.py（含相对导入）
# ---------------------------------------------------------------------------

_pkg = types.ModuleType("jev_gate_pkg")
_pkg.__path__ = [REPO_ROOT]
sys.modules["jev_gate_pkg"] = _pkg

_spec = importlib.util.spec_from_file_location(
    "jev_gate_pkg.plugin", os.path.join(REPO_ROOT, "plugin.py"))
_plugin_mod = importlib.util.module_from_spec(_spec)
sys.modules["jev_gate_pkg.plugin"] = _plugin_mod
_spec.loader.exec_module(_plugin_mod)

core = _plugin_mod.core  # 插件实际使用的 gate_core 模块（同一对象）

# ---------------------------------------------------------------------------
# 3. 网络护栏：任何联网尝试（urlopen / 建连 / DNS）都立刻失败
# ---------------------------------------------------------------------------

_orig_urlopen = urllib.request.urlopen
_orig_create_connection = socket.create_connection
_orig_getaddrinfo = socket.getaddrinfo


def _block_network(*args, **kwargs):
    raise AssertionError("network access attempted")


urllib.request.urlopen = _block_network
socket.create_connection = _block_network
socket.getaddrinfo = _block_network

# ---------------------------------------------------------------------------
# 4. 测试基础设施
# ---------------------------------------------------------------------------

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print("  %s %-58s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


class FakeLogger:
    """收集 (level, args) 元组的假 logger，供行为保持断言。"""

    def __init__(self):
        self.entries = []

    def info(self, *args):
        self.entries.append(("info", args))

    def warning(self, *args):
        self.entries.append(("warning", args))

    def debug(self, *args):
        self.entries.append(("debug", args))


class FakeConfig:
    """ctx.config 桩：async get，bot.nickname 返回「小助手」。"""

    async def get(self, key, default=None):
        if key == "bot.nickname":
            return "小助手"
        return default


class FakePaths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class FakeStatistics:
    pass


class FakeCtx:
    def __init__(self, data_dir):
        self.logger = FakeLogger()
        self.paths = FakePaths(data_dir)
        self.config = FakeConfig()
        self.statistics = FakeStatistics()


def _build_instance(data_dir):
    """构造真实插件实例 + 假 ctx + 真 GateSettings 模型。"""

    inst = _plugin_mod.JevTimingGatePlugin()
    inst.ctx = FakeCtx(data_dir)
    inst.config = _plugin_mod.GateSettings(
        plugin={"enabled": True, "api_key": "test-key",
                "endpoint": "https://example.invalid/v1/systemone"},
        gate={"bot_aliases": ["小助手"]},
    )
    return inst, inst.ctx


async def _run_maybe_gate(inst, kwargs):
    return await inst._maybe_gate(kwargs)


CHAT_ITEM = {"item_type": "UserMessageItem", "parts": [
    {"type": "text", "text": '<message user="甲">\n'},
    {"type": "text", "text": "最近一句聊天"},
]}

MENTION_ITEM = {"item_type": "UserMessageItem", "parts": [
    {"type": "text", "text": '<message user="甲">\n'},
    {"type": "text", "text": "@小助手 在吗"},
]}


def _chat_item(text):
    """构造一条带指定文本的普通聊天 Item（与 CHAT_ITEM 同构，便于造不同 state）。"""

    return {"item_type": "UserMessageItem", "parts": [
        {"type": "text", "text": '<message user="甲">\n'},
        {"type": "text", "text": text},
    ]}

# ---------------------------------------------------------------------------
# 5. 用例
# ---------------------------------------------------------------------------


def test_finalize():
    print("\n[1] finalize_gate_decision（五个分支）")
    check("no_result → continue/no_result",
          core.finalize_gate_decision({"reason": "no_result", "suppress": False}, shadow_mode=False),
          {"action": "continue", "reason": "no_result"})
    check("not_no_reply → continue/not_no_reply",
          core.finalize_gate_decision({"reason": "not_no_reply", "suppress": False}, shadow_mode=False),
          {"action": "continue", "reason": "not_no_reply"})
    check("suppress=False → continue/insufficient_evidence",
          core.finalize_gate_decision({"reason": "insufficient_evidence", "suppress": False}, shadow_mode=False),
          {"action": "continue", "reason": "insufficient_evidence"})
    check("suppress=True + 影子 → continue/shadow",
          core.finalize_gate_decision({"reason": "", "suppress": True}, shadow_mode=True),
          {"action": "continue", "reason": "shadow"})
    check("suppress=True 非影子 → suppress/suppress",
          core.finalize_gate_decision({"reason": "", "suppress": True}, shadow_mode=False),
          {"action": "suppress", "reason": "suppress"})


def test_plan():
    print("\n[2] plan_gate_action（九个分支）")
    base = {"enabled": True, "gate_enabled": True, "api_key": "k", "endpoint": "e",
            "mention_window_chars": 600}
    check("1. 未启用 → continue/disabled",
          core.plan_gate_action(cfg=dict(base, enabled=False), state_text="x", aliases=["小助手"]),
          {"action": "continue", "reason": "disabled", "decision": None})
    check("2. gate 未启用 → continue/gate_disabled",
          core.plan_gate_action(cfg=dict(base, gate_enabled=False), state_text="x", aliases=["小助手"]),
          {"action": "continue", "reason": "gate_disabled", "decision": None})
    check("3a. 缺 api_key → continue/no_credentials",
          core.plan_gate_action(cfg=dict(base, api_key=""), state_text="x", aliases=["小助手"]),
          {"action": "continue", "reason": "no_credentials", "decision": None})
    check("3b. 缺 endpoint → continue/no_credentials",
          core.plan_gate_action(cfg=dict(base, endpoint=""), state_text="x", aliases=["小助手"]),
          {"action": "continue", "reason": "no_credentials", "decision": None})
    check("4. 无别名 → continue/no_aliases",
          core.plan_gate_action(cfg=base, state_text="x", aliases=[]),
          {"action": "continue", "reason": "no_aliases", "decision": None})
    check("5. 空 state → continue/empty_state",
          core.plan_gate_action(cfg=base, state_text="", aliases=["小助手"]),
          {"action": "continue", "reason": "empty_state", "decision": None})
    check("6. @机器人 → continue/mentioned",
          core.plan_gate_action(cfg=base, state_text="最后一句 @小助手 在吗", aliases=["小助手"]),
          {"action": "continue", "reason": "mentioned", "decision": None})
    check("7. 熔断打开 → continue/breaker_open",
          core.plan_gate_action(cfg=base, state_text="x", aliases=["小助手"], breaker_open=True),
          {"action": "continue", "reason": "breaker_open", "decision": None})
    check("8. 命中去重缓存 → reuse/dedup_hit（decision 透传）",
          core.plan_gate_action(cfg=base, state_text="x", aliases=["小助手"],
                                cached_decision={"choice": "no_reply", "suppress": True}),
          {"action": "reuse", "reason": "dedup_hit", "decision": {"choice": "no_reply", "suppress": True}})
    check("9. 皆否 → call/call",
          core.plan_gate_action(cfg=base, state_text="x", aliases=["小助手"]),
          {"action": "call", "reason": "call", "decision": None})


def test_malformed():
    print("\n[3] 畸形输入（缺 mention_window_chars / 空 aliases / 空 state）")
    partial = {"enabled": True, "gate_enabled": True, "api_key": "k", "endpoint": "e"}
    try:
        r = core.plan_gate_action(cfg=partial, state_text="x", aliases=["小助手"])
        check("缺 mention_window_chars → 不抛异常，走 call", r["action"], "call")
    except Exception as exc:
        check("缺 mention_window_chars → 不抛异常（实际抛了 %s）" % repr(exc), "NO_EXC", "NO_EXC")
    check("空 aliases → no_aliases 干净返回",
          core.plan_gate_action(cfg=partial, state_text="x", aliases=[])["reason"], "no_aliases")
    check("空 state → empty_state 干净返回",
          core.plan_gate_action(cfg=partial, state_text="", aliases=["小助手"])["reason"], "empty_state")
    check("cfg 全空 → disabled（无键也干净返回）",
          core.plan_gate_action(cfg={}, state_text="", aliases=[])["reason"], "disabled")


def test_static():
    print("\n[4] 静态约束（gate_core 不依赖 SDK）")
    src = open(os.path.join(REPO_ROOT, "gate_core.py"), encoding="utf-8").read()
    check("gate_core.py 源码不含 'maibot_sdk'", "maibot_sdk" not in src, True)
    check("插件实际使用的 core 模块就是仓库 gate_core.py",
          os.path.abspath(core.__file__) == os.path.join(REPO_ROOT, "gate_core.py"), True)
    check("DedupCache 可从接缝模块导入（无 SDK）",
          callable(getattr(core, "DedupCache", None)), True)
    check("BreakerState 可从接缝模块导入（无 SDK）",
          callable(getattr(core, "BreakerState", None)), True)


def test_stale_state():
    print("\n[5] 去重 TTL / 熔断冷却（时钟注入，无墙钟依赖）")
    cache = core.DedupCache(capacity=8, ttl_seconds=100.0)
    cache.put("fp", {"choice": "no_reply"}, 0.0)
    check("TTL 内命中（t=99）", cache.get("fp", 99.0)["choice"], "no_reply")
    check("TTL 到期（t=100）→ 未命中", cache.get("fp", 100.0), None)

    s = core.BreakerState()
    for i in range(5):
        core.breaker_record(s, False, float(i))
    check("5 连败 → 熔断打开", core.breaker_should_skip(s, 5.0), True)
    check("冷却期内（t=100）→ 跳过", core.breaker_should_skip(s, 100.0), True)
    check("冷却到期（t=305）→ 关闭并放行", core.breaker_should_skip(s, 305.0), False)
    check("关闭后 opened_at 复位", s.opened_at, None)


def test_e2e():
    print("\n[6] 端到端 _maybe_gate（真实插件实例 + 计数桩 _ask_choice）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_e2e_")
    try:
        inst, ctx = _build_instance(data_dir)

        # ---- 6a. 抑制场景：no_reply + 高概率 → 改写请求，恰好调用 1 次 ----
        calls = []

        async def fake_ask_suppress(text, cfg, aliases):
            calls.append(("suppress", text))
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst._ask_choice = fake_ask_suppress
        kwargs = {"items": [CHAT_ITEM], "tool_definitions": [{"name": "tool_a"}]}
        out = asyncio.run(_run_maybe_gate(inst, kwargs))
        check("抑制：action=continue 且带 modified_kwargs", out["action"], "continue")
        check("抑制：modified_kwargs.items 长度 1",
              len(out["modified_kwargs"]["items"]), 1)
        check("抑制：tool_definitions 清空",
              out["modified_kwargs"]["tool_definitions"], [])
        check("抑制：_ask_choice 恰好调用 1 次", len(calls), 1)
        warns = [e for e in ctx.logger.entries if e[0] == "warning"]
        check("抑制：记录 1 条 warning", len(warns), 1)
        check("抑制：warning 文案与改动前一致（判定无需参与）",
              "判定无需参与" in warns[0][1][0], True)

        # ---- 6b. @机器人 场景：不调用 Jev，纯 continue ----
        ctx.logger.entries.clear()
        calls.clear()
        kwargs2 = {"items": [MENTION_ITEM]}
        out2 = asyncio.run(_run_maybe_gate(inst, kwargs2))
        check("@机器人：返回纯 continue", out2, {"action": "continue"})
        check("@机器人：_ask_choice 调用 0 次（必须不被调用）", len(calls), 0)
        infos = [e for e in ctx.logger.entries if e[0] == "info"]
        check("@机器人：记录 1 条 info", len(infos), 1)
        check("@机器人：info 文案与改动前一致",
              infos[0][1][0], "%s：检测到 @机器人，跳过门控，正常进入 Planner")

        # ---- 6c. choice=continue → 纯 continue，无 modified_kwargs ----
        ctx.logger.entries.clear()
        calls.clear()
        # 该 state 已被 6a 写入去重缓存：本场景要验证「每次都触网」的旧语义，
        # 故经插件的惰性访问器契约重置缓存（_dedup_cache=None → 下次访问重建）
        inst._dedup_cache = None

        async def fake_ask_continue(text, cfg, aliases):
            calls.append(("continue", text))
            return "continue", 0.95, {"no_reply": 0.05}

        inst._ask_choice = fake_ask_continue
        kwargs3 = {"items": [CHAT_ITEM]}
        out3 = asyncio.run(_run_maybe_gate(inst, kwargs3))
        check("choice=continue → 纯 continue（无 modified_kwargs）",
              out3["action"] == "continue" and out3.get("modified_kwargs") is None, True)
        check("choice=continue：_ask_choice 调用 1 次", len(calls), 1)
        not_no_reply = [e for e in ctx.logger.entries if e[0] == "info" and "conf=" in e[1][0]]
        check("choice=continue：记录 not_no_reply info 行", len(not_no_reply), 1)
        check("choice=continue：文案与改动前一致",
              not_no_reply[0][1][0], "%s：%s (conf=%s p_no_reply=%s)，正常进入 Planner")

        # ---- 6d. no_reply 低置信 → 证据不足，纯 continue ----
        ctx.logger.entries.clear()
        calls.clear()
        inst._dedup_cache = None  # 同上：6c 又把 CHAT_ITEM 写回了缓存

        async def fake_ask_weak(text, cfg, aliases):
            calls.append(("weak", text))
            return "no_reply", 0.50, {"no_reply": 0.55}

        inst._ask_choice = fake_ask_weak
        out4 = asyncio.run(_run_maybe_gate(inst, kwargs3))
        check("no_reply 低置信 → 纯 continue（证据不足）",
              out4["action"] == "continue" and out4.get("modified_kwargs") is None, True)
        weak = [e for e in ctx.logger.entries if e[0] == "info" and "证据不足" in e[1][0]]
        check("no_reply 低置信：记录证据不足 info 行", len(weak), 1)
        check("no_reply 低置信：文案与改动前一致",
              weak[0][1][0],
              "%s：no_reply 但证据不足 (%s=%.2f < %.2f; conf=%s p_no_reply=%s)，正常进入 Planner")

        # ---- 6e. 影子模式：本该抑制但不改写 ----
        inst2, ctx2 = _build_instance(data_dir)
        inst2.config = _plugin_mod.GateSettings(
            plugin={"enabled": True, "api_key": "test-key",
                    "endpoint": "https://example.invalid/v1/systemone", "shadow_mode": True},
            gate={"bot_aliases": ["小助手"]},
        )
        calls2 = []

        async def fake_ask2(text, cfg, aliases):
            calls2.append(text)
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst2._ask_choice = fake_ask2
        out5 = asyncio.run(_run_maybe_gate(inst2, kwargs3))
        check("影子：返回纯 continue，不改写", out5, {"action": "continue"})
        check("影子：_ask_choice 调用 1 次", len(calls2), 1)
        shadow = [e for e in ctx2.logger.entries if e[0] == "info" and "影子" in e[1][0]]
        check("影子：记录 [影子] info 行", len(shadow), 1)
        check("影子：文案与改动前一致",
              shadow[0][1][0],
              "%s[影子]：本该抑制（%s=%.2f >= %.2f; conf=%s）items=%s→1 tools=%s→0，本轮不改行为")
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_config_get():
    print("\n[8] ctx.config.get 现代写法（todo 9）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_cfgget_")
    try:
        # ---- 8a. 非空昵称：无手填别名 → _resolve_aliases 走 config.get ----
        inst, ctx = _build_instance(data_dir)
        inst.config = _plugin_mod.GateSettings(
            plugin={"enabled": True, "api_key": "test-key",
                    "endpoint": "https://example.invalid/v1/systemone"},
            gate={"bot_aliases": []},
        )
        aliases, source = asyncio.run(inst._resolve_aliases([]))
        check("config.get 非空 → (['小助手'], '自动读主程序配置 bot.nickname')",
              (aliases, source), (["小助手"], "自动读主程序配置 bot.nickname"))

        # ---- 8b. 空昵称：config.get 返回 "" → 拒绝启用 ----
        class EmptyConfig:
            async def get(self, key, default=None):
                return "" if key == "bot.nickname" else default

        inst2, ctx2 = _build_instance(data_dir)
        inst2.config = _plugin_mod.GateSettings(
            plugin={"enabled": True, "api_key": "test-key",
                    "endpoint": "https://example.invalid/v1/systemone"},
            gate={"bot_aliases": []},
        )
        inst2.ctx.config = EmptyConfig()
        aliases2, source2 = asyncio.run(inst2._resolve_aliases([]))
        check("config.get 空 → ([], '未获取到')", (aliases2, source2), ([], "未获取到"))

        # 门控必须拒绝启用：_maybe_gate 返回纯 continue，_ask_choice 调用 0 次
        # （_alias_refresh_ts 拨到过去，逼 _aliases_for_gate 真正走 _resolve_aliases）
        inst2._alias_refresh_ts = -1000.0
        calls = []

        async def fake_ask(text, cfg, aliases):
            calls.append(text)
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst2._ask_choice = fake_ask
        kwargs = {"items": [CHAT_ITEM]}
        out = asyncio.run(_run_maybe_gate(inst2, kwargs))
        check("空昵称 → 门控拒绝启用：返回纯 continue", out, {"action": "continue"})
        check("空昵称 → _ask_choice 调用 0 次", len(calls), 0)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_key_paths():
    print("\n[9] 密钥位置：data_dir 优先 + 旧路径兼容 + 一次性告警（todo 8）")
    repo_key = os.path.join(REPO_ROOT, "jev_config.json")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_key_")
    try:
        # ---- 9a. 仅旧路径（插件源码目录）存在 → 取到 key + 一次性告警 ----
        with open(repo_key, "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-legacy-001"}, fh)
        inst, ctx = _build_instance(data_dir)
        check("仅旧文件 → _private_key() 取到 key", inst._private_key(), "sk-legacy-001")
        check("仅旧文件 → _legacy_key_warned 置位",
              getattr(inst, "_legacy_key_warned", False), True)
        warns = [e for e in ctx.logger.entries if e[0] == "warning"]
        check("仅旧文件 → 恰好 1 条 warning", len(warns), 1)
        check("仅旧文件 → warning 含新位置提示", "data/plugins" in warns[0][1][0], True)
        check("仅旧文件 → warning 不含密钥本身", "sk-legacy-001" not in warns[0][1][0], True)

        # ---- 9b. 二次调用 → 仍取到 key，无新增告警（一次性守卫） ----
        ctx.logger.entries.clear()
        check("二次调用 → 仍取到 key", inst._private_key(), "sk-legacy-001")
        warns2 = [e for e in ctx.logger.entries if e[0] == "warning"]
        check("二次调用 → 无新增 warning", len(warns2), 0)

        # ---- 9c. 仅 data_dir 文件存在 → key + 零 legacy 告警 ----
        ctx.logger.entries.clear()
        os.remove(repo_key)
        data_key = os.path.join(data_dir, "jev_config.json")
        with open(data_key, "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-data-002"}, fh)
        inst2, ctx2 = _build_instance(data_dir)
        check("仅 data_dir → _private_key() 取到 key", inst2._private_key(), "sk-data-002")
        warns3 = [e for e in ctx2.logger.entries if e[0] == "warning"]
        check("仅 data_dir → 零 legacy warning", len(warns3), 0)

        # ---- 9d. 两处都存在 → data_dir 值胜出 ----
        with open(repo_key, "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-legacy-003"}, fh)
        with open(data_key, "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-data-004"}, fh)
        inst3, ctx3 = _build_instance(data_dir)
        check("两处都在 → data_dir 值胜出", inst3._private_key(), "sk-data-004")
        warns4 = [e for e in ctx3.logger.entries if e[0] == "warning"]
        check("两处都在 → 零 legacy warning（data_dir 命中）", len(warns4), 0)

        # ---- 9e. 都不存在 → ""，无异常无告警 ----
        ctx3.logger.entries.clear()
        os.remove(repo_key)
        os.remove(data_key)
        check("两处都无 → _private_key() 返回空串", inst3._private_key(), "")
        warns5 = [e for e in ctx3.logger.entries if e[0] == "warning"]
        check("两处都无 → 无 warning", len(warns5), 0)

        # ---- 9f. ctx.paths.data_dir 访问抛异常 → ""，不崩溃 ----
        class BadPaths:
            @property
            def data_dir(self):
                raise RuntimeError("paths unavailable")

        inst4, ctx4 = _build_instance(data_dir)
        inst4.ctx.paths = BadPaths()
        check("paths.data_dir 抛异常 → 返回空串", inst4._private_key(), "")
        warns6 = [e for e in ctx4.logger.entries if e[0] == "warning"]
        check("paths.data_dir 抛异常 → 无 warning", len(warns6), 0)

        # ---- 9g. 畸形 JSON → ""，无异常 ----
        with open(data_key, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        inst5, ctx5 = _build_instance(data_dir)
        check("畸形 JSON → 返回空串", inst5._private_key(), "")
    finally:
        if os.path.exists(repo_key):
            os.remove(repo_key)
        shutil.rmtree(data_dir, ignore_errors=True)


def test_log_dump():
    print("\n[7] 行为保持证据：日志元组 dump（供评审对照改动前文案）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_dump_")
    try:
        inst, ctx = _build_instance(data_dir)

        async def fake_ask(text, cfg, aliases):
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst._ask_choice = fake_ask

        # 抑制轮（cached-suppress 语义：判定进入抑制分支）
        kwargs = {"items": [CHAT_ITEM], "tool_definitions": [{"name": "tool_a"}]}
        asyncio.run(_run_maybe_gate(inst, kwargs))
        for level, args in ctx.logger.entries:
            print("  LOGTUPLE suppress %s %r" % (level, args))

        # @机器人轮（mentioned：确定性豁免）
        ctx.logger.entries.clear()
        kwargs2 = {"items": [MENTION_ITEM]}
        asyncio.run(_run_maybe_gate(inst, kwargs2))
        for level, args in ctx.logger.entries:
            print("  LOGTUPLE mentioned %s %r" % (level, args))

        check("dump：日志元组已输出（无墙钟/uuid 依赖）", True, True)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_dedup_reuse():
    print("\n[10] 每轮去重：同 state 仅 1 次 Jev 调用；不同 state 再调（todo 6）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_dedup_")
    try:
        inst, ctx = _build_instance(data_dir)
        calls = []

        async def fake_ask_suppress(text, cfg, aliases):
            calls.append(text)
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst._ask_choice = fake_ask_suppress

        # ---- 1. 同 state 两次 → 恰好 1 次调用，第二次复用 ----
        kw1 = {"items": [CHAT_ITEM], "tool_definitions": [{"name": "tool_a"}]}
        kw2 = {"items": [CHAT_ITEM], "tool_definitions": [{"name": "tool_a"}]}
        out1 = asyncio.run(_run_maybe_gate(inst, kw1))
        check("同 state 第 1 次：action=continue 且带 modified_kwargs", out1["action"], "continue")
        check("同 state 第 1 次：modified_kwargs.items 长度 1",
              len(out1["modified_kwargs"]["items"]), 1)
        check("同 state 第 1 次：tool_definitions 清空",
              out1["modified_kwargs"]["tool_definitions"], [])
        print("    calls_before_second=%d" % len(calls))
        out2 = asyncio.run(_run_maybe_gate(inst, kw2))
        print("    calls_after_second=%d" % len(calls))
        check("同 state 第 2 次：action=continue 且带 modified_kwargs", out2["action"], "continue")
        check("同 state 第 2 次：modified_kwargs.items 长度 1",
              len(out2["modified_kwargs"]["items"]), 1)
        check("同 state 第 2 次：tool_definitions 清空",
              out2["modified_kwargs"]["tool_definitions"], [])
        check("同 state 两次 → _ask_choice 恰好调用 1 次", len(calls), 1)
        infos = [e for e in ctx.logger.entries if e[0] == "info"]
        reuse_lines = [a for a in infos if "复用本轮判定" in a[1][0]]
        check("第 2 次日志含「复用本轮判定」", len(reuse_lines), 1)
        breaker = inst._breaker()
        check("复用路径不记录熔断失败（failure_count=0）", breaker.failure_count, 0)
        check("复用路径熔断未打开（opened_at=None）", breaker.opened_at, None)

        # ---- 2. 不同 state → 第 2 次调用发生 ----
        ctx.logger.entries.clear()
        out3 = asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("完全不同的另一句")]}))
        check("不同 state → 返回 continue（新判定）", out3["action"], "continue")
        check("不同 state → _ask_choice 计数 1→2", len(calls), 2)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_breaker():
    print("\n[11] 熔断：连续失败打开 → 阻塞不再调用 → 冷却到期关闭（todo 6）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_breaker_")
    try:
        inst, ctx = _build_instance(data_dir)
        calls = []
        threshold = _plugin_mod.BREAKER_THRESHOLD

        async def fake_ask_fail(text, cfg, aliases):
            calls.append(text)
            return None, None, {}

        inst._ask_choice = fake_ask_fail

        # ---- 3a. threshold-1 次失败：熔断仍未打开 ----
        for i in range(threshold - 1):
            out = asyncio.run(_run_maybe_gate(
                inst, {"items": [_chat_item("失败状态%d" % i)]}))
            check("第 %d 次失败 → 纯 continue（无改写）" % (i + 1),
                  out["action"] == "continue" and out.get("modified_kwargs") is None, True)
        check("threshold-1 次失败 → _ask_choice 调用 %d 次" % (threshold - 1),
              len(calls), threshold - 1)
        check("threshold-1 次失败 → 熔断仍关闭", inst._breaker().opened_at, None)

        # ---- 3b. 第 threshold 次失败：熔断打开 ----
        out = asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("失败状态开")]}))
        check("第 %d 次失败 → 纯 continue" % threshold,
              out["action"] == "continue" and out.get("modified_kwargs") is None, True)
        check("第 %d 次失败 → _ask_choice 调用 %d 次" % (threshold, threshold),
              len(calls), threshold)
        check("第 %d 次失败 → 熔断打开" % threshold, inst._breaker().opened_at is not None, True)

        # ---- 3c. 熔断打开后：不再调用，裸 continue，日志含「熔断」 ----
        ctx.logger.entries.clear()
        out = asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("熔断期内新状态")]}))
        check("熔断打开 → 返回裸 continue（无 modified_kwargs）", out, {"action": "continue"})
        check("熔断打开 → _ask_choice 未再调用（次数不变）", len(calls), threshold)
        warns = [e for e in ctx.logger.entries if e[0] == "warning"]
        breaker_warns = [a for a in warns if "熔断" in a[1][0]]
        check("熔断打开 → 日志含「熔断」", len(breaker_warns), 1)

        # ---- 4. 冷却到期（回拨 opened_at）→ 关闭并重新调用 ----
        inst._breaker().opened_at -= (_plugin_mod.BREAKER_COOLDOWN_SECONDS + 1)
        out = asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("冷却后新状态")]}))
        check("冷却到期 → 熔断关闭", inst._breaker().opened_at, None)
        check("冷却到期 → _ask_choice 再次调用（次数 +1）", len(calls), threshold + 1)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_breaker_reset():
    print("\n[12] 熔断：一次成功归零（连续失败语义，todo 6）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_bres_")
    try:
        inst, ctx = _build_instance(data_dir)
        calls = []
        threshold = _plugin_mod.BREAKER_THRESHOLD
        fail_result = (None, None, {})

        async def fake_ask(text, cfg, aliases):
            calls.append(text)
            return fail_result

        inst._ask_choice = fake_ask

        for i in range(threshold - 1):
            asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("前置失败%d" % i)]}))
        check("threshold-1 次失败 → 熔断未打开", inst._breaker().opened_at, None)
        check("threshold-1 次失败 → failure_count=%d" % (threshold - 1),
              inst._breaker().failure_count, threshold - 1)

        # 一次成功 → 计数归零
        fail_result = ("no_reply", 0.90, {"no_reply": 0.95})
        asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("成功状态")]}))
        check("一次成功 → failure_count 归零", inst._breaker().failure_count, 0)
        check("一次成功 → 熔断未打开", inst._breaker().opened_at, None)

        # 再 threshold-1 次失败 → 仍关闭（连续失败，非累计）
        fail_result = (None, None, {})
        for i in range(threshold - 1):
            asyncio.run(_run_maybe_gate(inst, {"items": [_chat_item("后置失败%d" % i)]}))
        check("成功后再次 threshold-1 次失败 → failure_count=%d" % (threshold - 1),
              inst._breaker().failure_count, threshold - 1)
        check("成功后再次失败 → 熔断仍关闭（非累计）", inst._breaker().opened_at, None)
        check("总调用次数=%d" % (2 * (threshold - 1) + 1),
              len(calls), 2 * (threshold - 1) + 1)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_dedup_ttl_expiry():
    print("\n[13] 去重 TTL 过期：同 state 再次调用桩（todo 6）")
    data_dir = tempfile.mkdtemp(prefix="jev_gate_ttl_")
    try:
        inst, ctx = _build_instance(data_dir)
        calls = []

        async def fake_ask(text, cfg, aliases):
            calls.append(text)
            return "no_reply", 0.90, {"no_reply": 0.95}

        inst._ask_choice = fake_ask
        item = _chat_item("TTL 测试句")
        kwargs = {"items": [item]}

        out1 = asyncio.run(_run_maybe_gate(inst, kwargs))
        check("TTL 内：第 1 次返回 continue+modified_kwargs", out1["action"], "continue")
        check("TTL 内：_ask_choice 调用 1 次", len(calls), 1)

        # 证明 key 计算与插件一致：缓存里能取到该 state 的判定
        cfg = inst._cfg()
        state_text = core.extract_planner_state_text([item], max_chars=cfg["state_max_chars"])
        key = core.turn_fingerprint(state_text)
        check("缓存里存在该 state 的判定（key 与插件一致）",
              inst._turn_dedup().get(key, 0.0) is not None, True)

        # 用插件的访问器把该条目改成「早已过期」：now 注入到很久以前
        inst._turn_dedup().put(key, {"choice": "no_reply"}, -1e9)

        out2 = asyncio.run(_run_maybe_gate(inst, {"items": [item]}))
        check("TTL 过期：同 state 再次调用 → continue+modified_kwargs", out2["action"], "continue")
        check("TTL 过期：同 state 再次调用 → _ask_choice 计数 1→2", len(calls), 2)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_finalize()
        test_plan()
        test_malformed()
        test_static()
        test_stale_state()
        test_e2e()
        test_config_get()
        test_key_paths()
        test_log_dump()
        test_dedup_reuse()
        test_breaker()
        test_breaker_reset()
        test_dedup_ttl_expiry()
        print("\n通过 %d / 失败 %d" % (PASS, FAIL))
    finally:
        urllib.request.urlopen = _orig_urlopen
        socket.create_connection = _orig_create_connection
        socket.getaddrinfo = _orig_getaddrinfo
    sys.exit(1 if FAIL else 0)

