"""Jev 参与门控（timing gate）。

在 `maisaka.planner.before_request` 之前做一次**廉价判断**：用决策模型 Jev 判
「本轮是否值得进入完整 planner」，高置信判「无需参与」时抑制这一轮，省下一次 planner 调用。

**开箱即用**：装好后只需要填两个东西——Jev 的 `endpoint` 与 `api_key`。
机器人昵称（@豁免用）通过官方 `config.get` 能力读主程序的 `bot.nickname`；读不到时在
`[gate] bot_aliases` 手填（拿不到昵称时门控拒绝启用，免得被点名时被静默抑制）。

**多提供商**：`api_style` 支持三种接口形状，指向任意提供 Jev 的网关都能用：
  · `typesafe`（默认）—— TypeSafe 原生 Choice 原语（`state` + `questions`）
  · `classifier_dev` —— classifier.dev 风格（`inputs` + `labels` + `instructions`）
  · `openai_json` —— 任何 OpenAI 兼容网关，要求模型回一段 JSON

三个关键设计（都有实测依据，见 README 的「设计依据」）：
1. 控制流用 `probabilities["no_reply"]`，**不用 confidence**——前者重复调用的抖动只有一半。
2. @机器人 走**确定性豁免**，且只扫 state 尾部窗口——直接点名时 Jev 基本是瞎的（p≈0.5），
   而扫全窗又会被历史提及污染。
3. 抑制的方式是**改写**：把本轮的 items 换成一条「本轮无需参与」提示，并清空 tool_definitions，
   完整 planner 的历史上下文与工具都不再参与这一轮。不读宿主文件，也不改宿主核心。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from datetime import datetime
from typing import Any, Mapping

from maibot_sdk import HomeCard, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from . import gate_core as core
from .config import GateSettings

PRIVATE_KEY_FILE = "jev_config.json"
HOOK_TIMEOUT_MS = 8000
LOG_TAG = "Jev 门控"
ALIAS_RETRY_SECONDS = 60.0
DEDUP_CAPACITY = 256          # 去重缓存容量（有界 LRU）
DEDUP_TTL_SECONDS = 120.0     # 同一内容指纹在此秒数内视为同一轮
BREAKER_THRESHOLD = 5         # 连续失败达到此值 → 打开熔断
BREAKER_COOLDOWN_SECONDS = 300.0  # 熔断打开后的冷却秒数
DECISION_RECORD_FILE = "gate_decisions.jsonl"  # 判定记录（JSONL：只记判断结果，不含聊天正文）

#: 首页卡片的注册期兜底内容。
#: 文档（docs/zh/plugin/home-cards.md:53）只描述「卡片内容」，示例是注册期静态列表，
#: 并未文档化"运行时内容提供者"；因此这里给静态描述块，并由被装饰的
#: :meth:`JevTimingGatePlugin.home_card` 额外返回实时聚合块——宿主若采用方法返回值
#: 即展示实时数值，否则回落到这份静态描述。两者都只用文档支持的内容块，不含 HTML/JS。
_STATIC_CARD_CONTENT: list[dict[str, Any]] = [
    {
        "type": "markdown",
        "content": "**Jev 参与门控**：用 Jev 判定本轮是否值得进入完整 planner，"
                   "高置信判「无需参与」时把这一轮改写成极简请求。"
                   "统计来自本地判定记录，**不含聊天正文**。",
    },
    {
        "type": "key_value",
        "entries": {"记录文件": DECISION_RECORD_FILE, "位置": "插件数据目录（ctx.paths.data_dir）"},
    },
]


def _append_line(path: str, line: str) -> None:
    """把一行 JSON 追加到 JSONL 文件（同步 helper，由调用方经 asyncio.to_thread 执行）。"""

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class JevTimingGatePlugin(MaiBotPlugin):
    """按 Jev 判定结果抑制「无需参与」的 planner 轮次。"""

    config_model = GateSettings

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        cfg = self._cfg()
        aliases, source = await self._resolve_aliases(cfg.get("bot_aliases") or [])
        self._alias_cache = (aliases, source)
        self.ctx.logger.info(
            "%s已加载：enabled=%s shadow=%s 风格=%s 端点=%s 概率阈值=%s 别名=%s（来源：%s）窗口=%s字",
            LOG_TAG, cfg.get("enabled"), cfg.get("shadow_mode"), cfg.get("api_style"),
            cfg.get("endpoint"), cfg.get("probability_threshold"),
            aliases or "—", source, cfg.get("mention_window_chars"),
        )
        if cfg.get("enabled") and not cfg.get("api_key"):
            self.ctx.logger.warning("%s：还没填 api_key（插件配置与 %s 都为空）→ 判定一律放行。",
                                    LOG_TAG, PRIVATE_KEY_FILE)
        if cfg.get("enabled") and not aliases:
            self.ctx.logger.warning(
                "%s：拿不到机器人昵称（config.get 读不到主程序的 bot.nickname）→ 门控**拒绝启用**，"
                "以免在你被 @ 时被静默抑制。请在 [gate] bot_aliases 手动填机器人昵称。",
                LOG_TAG,
            )

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        cfg = self._cfg()
        aliases, source = await self._resolve_aliases(cfg.get("bot_aliases") or [])
        self._alias_cache = (aliases, source)
        self.ctx.logger.info("%s：配置已更新（scope=%s）别名=%s（来源：%s）",
                             LOG_TAG, scope, aliases or "—", source)

    # ------------------------------------------------------------------ 配置
    def _cfg(self) -> dict[str, Any]:
        """读取并归一化配置；任何异常都返回空字典（调用方按"未启用"处理）。"""

        try:
            settings = self.config
            plugin, gate = settings.plugin, settings.gate
        except Exception as exc:  # 配置未注入 / 校验失败都不能拖垮 planner
            self.ctx.logger.debug("%s：读取配置失败（按未启用处理）: %s", LOG_TAG, exc)
            return {}

        style = core.normalize_style(str(plugin.api_style or "typesafe"))
        auth_header, auth_prefix = core.resolve_auth(
            style,
            getattr(plugin, "auth_header", "") or "",
            getattr(plugin, "auth_prefix", "") or "",
        )
        aliases = [str(a).strip() for a in (gate.bot_aliases or []) if str(a).strip()]
        return {
            "enabled": bool(plugin.enabled),
            "shadow_mode": bool(plugin.shadow_mode),
            "api_key": (str(plugin.api_key or "").strip() or self._private_key()),
            "endpoint": str(plugin.endpoint or core.DEFAULT_ENDPOINTS.get(style, "")).strip(),
            "api_style": style,
            "auth_header": auth_header,
            "auth_prefix": auth_prefix,
            "model": str(plugin.model or "jev-latest").strip() or "jev-latest",
            "timeout_sec": float(plugin.timeout_sec or 6.0),
            "state_max_chars": int(plugin.state_max_chars or 3000),
            "gate_enabled": bool(gate.enabled),
            "probability_threshold": float(gate.probability_threshold or 0.80),
            "confidence_fallback_threshold": float(gate.confidence_fallback_threshold or 0.62),
            "bot_aliases": aliases,
            "mention_window_chars": int(gate.mention_window_chars or core.DEFAULT_MENTION_WINDOW_CHARS),
        }

    def _read_key_file(self, path: str) -> str:
        """从指定 jev_config.json 读 key；任何读/解析异常都返回空串（调用方按「未配置」处理）。"""

        try:
            with open(path, encoding="utf-8") as fh:
                return str(json.load(fh).get("api_key") or "").strip()
        except Exception:
            return ""

    def _preferred_key_path(self) -> str:
        """解析首选密钥位置：ctx.paths.data_dir（宿主规范持久化目录）；ctx/paths 缺失不崩溃。"""

        try:
            return os.path.join(str(self.ctx.paths.data_dir), PRIVATE_KEY_FILE)
        except Exception:
            return ""

    def _decision_record_path(self) -> str:
        """解析判定记录文件路径：ctx.paths.data_dir（宿主规范持久化目录）；ctx/paths 缺失不崩溃。"""

        try:
            return os.path.join(str(self.ctx.paths.data_dir), DECISION_RECORD_FILE)
        except Exception:
            return ""

    def _private_key(self) -> str:
        """从 jev_config.json 读 key（便于用 600 权限把密钥隔离在配置界面之外）。

        优先读宿主规范位置 ``ctx.paths.data_dir``（``data/plugins/<plugin_id>/``）；
        旧插件源码目录下的同名文件仍兼容读取，命中旧路径时**一次性**告警提示迁移。
        两处都读不到（或文件损坏）返回空串、绝不抛异常——调用方按「未配置」放行。
        """

        preferred = self._preferred_key_path()
        if preferred:
            key = self._read_key_file(preferred)
            if key:
                return key
        legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), PRIVATE_KEY_FILE)
        key = self._read_key_file(legacy)
        if key and not getattr(self, "_legacy_key_warned", False):
            self._legacy_key_warned = True
            self.ctx.logger.warning(
                "%s：已从旧位置（插件源码目录）读取 %s。宿主规范的新位置是 "
                "data/plugins/<plugin_id>/ 下的同名文件，请把该文件迁移过去"
                "（文档明确不再推荐旧式 plugins/<plugin>/data 目录）。",
                LOG_TAG, PRIVATE_KEY_FILE,
            )
        return key

    # ------------------------------------------------------------------ 昵称（@豁免用）
    async def _resolve_aliases(self, configured: list[str]) -> tuple[list[str], str]:
        """确定 @豁免 用的昵称：优先用户手填，否则通过官方 config.get 读主程序配置。"""

        if configured:
            return configured, "插件配置手填"
        try:
            # `ctx.config.get` 是文档化的配置读取入口，直接返回配置值，
            # 不需要再从 RPC 结果里取 `value` 字段
            # （docs/zh/plugin/config.md:317-330、docs/zh/plugin/api-reference.md:258-290）。
            # `config.get` 读的是**全局**配置项（docs/zh/develop/plugin-runtime-internals.md:243），
            # 因此 `bot.nickname` 取的是主程序机器人昵称。
            name = str(await self.ctx.config.get("bot.nickname", "") or "").strip()
        except Exception as exc:
            self.ctx.logger.debug("%s：config.get(bot.nickname) 调用失败: %s", LOG_TAG, exc)
            return [], "未获取到"
        if name:
            return [name], "自动读主程序配置 bot.nickname"
        return [], "未获取到"

    async def _aliases_for_gate(self, cfg: dict[str, Any]) -> list[str]:
        """给门控用的别名：手填优先；否则用缓存，缓存空则限频重试解析。"""

        configured = cfg.get("bot_aliases") or []
        if configured:
            return configured
        cached, _source = getattr(self, "_alias_cache", ([], ""))
        if cached:
            return cached
        now = asyncio.get_running_loop().time()
        if now - getattr(self, "_alias_refresh_ts", 0.0) < ALIAS_RETRY_SECONDS:
            return []
        self._alias_refresh_ts = now
        aliases, source = await self._resolve_aliases([])
        if aliases:
            self._alias_cache = (aliases, source)
        return aliases

    # ------------------------------------------------------------------ Jev 调用
    async def _ask_choice(
        self, text: str, cfg: dict[str, Any], bot_aliases: list[str]
    ) -> tuple[str | None, float | None, dict[str, Any]]:
        """按配置的接口风格询问 Jev，归一成 (choice, confidence, probabilities)。"""

        body = core.build_request(
            cfg["api_style"],
            text=text,
            model=cfg["model"],
            bot_name=bot_aliases[0] if bot_aliases else "",
        )
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = cfg["endpoint"]
        api_key, timeout = cfg["api_key"], cfg["timeout_sec"]
        auth_header, auth_prefix = cfg["auth_header"], cfg["auth_prefix"]

        if not url:
            self.ctx.logger.warning(
                "%s：api_style=%s 需要填 endpoint，当前为空 → 本轮放行", LOG_TAG, cfg["api_style"])
            return None, None, {}

        def _call() -> dict[str, Any]:
            headers = {
                "Content-Type": "application/json",
                # 部分网关（Cloudflare 前置）缺 UA 会直接 403 且不返回 JSON
                "User-Agent": "maibot-jev-timing-gate/1.0",
            }
            if auth_header and api_key:
                headers[auth_header] = f"{auth_prefix}{api_key}"
            request = urllib.request.Request(url, data=payload_bytes, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))

        try:
            payload = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout + 0.5)
        except Exception as exc:
            self.ctx.logger.warning("%s：判定调用失败（已忽略，正常进入 Planner）: %s", LOG_TAG, exc)
            return None, None, {}
        return core.parse_response(cfg["api_style"], payload)

    # ------------------------------------------------------------------ 去重 / 熔断状态（惰性：不依赖 on_load 已运行）
    def _turn_dedup(self) -> core.DedupCache:
        cache = getattr(self, "_dedup_cache", None)
        if cache is None:
            cache = core.DedupCache(capacity=DEDUP_CAPACITY, ttl_seconds=DEDUP_TTL_SECONDS)
            self._dedup_cache = cache
        return cache

    def _breaker(self) -> core.BreakerState:
        state = getattr(self, "_breaker_state", None)
        if state is None:
            state = core.BreakerState(threshold=BREAKER_THRESHOLD, cooldown_seconds=BREAKER_COOLDOWN_SECONDS)
            self._breaker_state = state
        return state

    # ------------------------------------------------------------------ 门控主体
    async def _record_decision(self, decision: Mapping[str, Any] | None, *, action: str,
                               reason: str, reused: bool, breaker_skip: bool) -> None:
        """把一次判定追加到 ctx.paths.data_dir 的 JSONL 记录文件。

        磁盘 IO 走 asyncio.to_thread，绝不在事件循环里同步读写；
        任何写入失败只记日志、**不得**影响门控结果（整体 try/except 包住）。
        """

        try:
            path = self._decision_record_path()
            if not path:
                return
            payload = dict(decision or {})
            if reason:
                payload["reason"] = reason
            record = core.build_decision_record(
                payload,
                action=action,
                reused=reused,
                breaker_skip=breaker_skip,
                timestamp=datetime.now().isoformat(timespec="seconds"),
            )
            await asyncio.to_thread(_append_line, path, json.dumps(record, ensure_ascii=False))
        except Exception as exc:
            self.ctx.logger.warning("%s：判定记录写入失败（已忽略，不影响门控）: %s", LOG_TAG, exc)

    async def _maybe_gate(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.get("enabled") or not cfg.get("gate_enabled"):
            return {"action": "continue"}
        if not cfg.get("api_key") or not cfg.get("endpoint"):
            return {"action": "continue"}
        aliases = await self._aliases_for_gate(cfg)
        if not aliases:
            # 安全默认：拿不到机器人昵称就不动手，避免"被点名时被静默抑制"这种不可见的坏结果
            return {"action": "continue"}

        items = kwargs.get("items")
        state_text = core.extract_planner_state_text(items, max_chars=cfg["state_max_chars"])

        # ---- ① 纯决策接缝：给定已解析 cfg + state + 去重/熔断状态 → 下一步动作（不 I/O）----
        now = asyncio.get_running_loop().time()
        breaker = self._breaker()
        breaker_open = core.breaker_should_skip(breaker, now)
        dedup_key = core.turn_fingerprint(state_text)
        cached = self._turn_dedup().get(dedup_key, now)
        plan = core.plan_gate_action(
            cfg=cfg, state_text=state_text, aliases=aliases,
            cached_decision=cached, breaker_open=breaker_open,
        )
        if plan["action"] == core.GATE_ACTION_CONTINUE:
            if plan["reason"] == "mentioned":
                self.ctx.logger.info("%s：检测到 @机器人，跳过门控，正常进入 Planner", LOG_TAG)
            elif plan["reason"] == "breaker_open":
                remaining = 0.0
                if breaker.opened_at is not None:
                    remaining = max(0.0, breaker.opened_at + breaker.cooldown_seconds - now)
                self.ctx.logger.warning(
                    "%s：判定端点连续失败已熔断，冷却剩余 %.0f 秒，本轮跳过判定直接放行",
                    LOG_TAG, remaining,
                )
                await self._record_decision(
                    None, action="continue", reason="breaker_open",
                    reused=False, breaker_skip=True,
                )
            # 其余 reason（disabled/gate_disabled/no_credentials/no_aliases/empty_state）
            # 与改动前一致：静默放行
            return {"action": "continue"}
        if plan["action"] == core.GATE_ACTION_REUSE:
            # 复用上一轮判定（去重命中）：不再调用 Jev，也不记录熔断
            decision = dict(plan["decision"] or {})
            self.ctx.logger.info("%s[复用]：复用本轮判定，不重复调用 Jev", LOG_TAG)
        else:
            # ---- ② Jev 判定 ----
            choice, confidence, probabilities = await self._ask_choice(state_text, cfg, aliases)
            call_ok = choice is not None
            core.breaker_record(breaker, call_ok, now)
            decision = core.evaluate_decision(
                choice,
                confidence,
                probabilities,
                probability_threshold=cfg["probability_threshold"],
                confidence_fallback_threshold=cfg["confidence_fallback_threshold"],
            )
            decision = dict(decision, choice=choice)
            if call_ok and decision.get("reason") != "no_result":
                self._turn_dedup().put(dedup_key, dict(decision), now)
        conf_text, p_text = decision["conf_text"], decision["p_text"]
        original_items = len(items) if isinstance(items, list) else 0
        original_tools = len(kwargs.get("tool_definitions") or [])

        # ---- ③ 收尾判定：继续 或 抑制 ----
        outcome = core.finalize_gate_decision(decision, shadow_mode=bool(cfg.get("shadow_mode")))
        await self._record_decision(
            decision,
            action=outcome["action"],
            reason=outcome["reason"],
            reused=plan["action"] == core.GATE_ACTION_REUSE,
            breaker_skip=False,
        )
        if outcome["action"] == core.GATE_ACTION_CONTINUE:
            if outcome["reason"] == "not_no_reply":
                self.ctx.logger.info(
                    "%s：%s (conf=%s p_no_reply=%s)，正常进入 Planner",
                    LOG_TAG, decision.get("choice"), conf_text, p_text,
                )
            elif outcome["reason"] == "insufficient_evidence":
                self.ctx.logger.info(
                    "%s：no_reply 但证据不足 (%s=%.2f < %.2f; conf=%s p_no_reply=%s)，正常进入 Planner",
                    LOG_TAG, decision["evidence"], decision["value"], decision["limit"], conf_text, p_text,
                )
            elif outcome["reason"] == "shadow":
                self.ctx.logger.info(
                    "%s[影子]：本该抑制（%s=%.2f >= %.2f; conf=%s）items=%s→1 tools=%s→0，本轮不改行为",
                    LOG_TAG, decision["evidence"], decision["value"], decision["limit"],
                    conf_text, original_items, original_tools,
                )
            # no_result 与其它 reason：保持静默，与改动前一致
            return {"action": "continue"}

        # ---- ④ 抑制：改写本轮请求 ----
        kwargs["items"] = [core.build_gate_skip_item()]
        if "tool_definitions" in kwargs:
            kwargs["tool_definitions"] = []
        self.ctx.logger.warning(
            "%s：判定无需参与(%s=%.2f >= %.2f; conf=%s)，本轮已改写成极简请求"
            "（只留一条跳过提示、不带工具） items=%s->1 tools=%s->0",
            LOG_TAG, decision["evidence"], decision["value"], decision["limit"],
            conf_text, original_items, original_tools,
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    # ------------------------------------------------------------------ 首页卡片
    def _read_records_text(self) -> str:
        """同步读判定记录全文；文件缺失/不可读一律返回空串（卡片退化为零值）。"""

        path = self._decision_record_path()
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except Exception:
            return ""

    async def _home_card_blocks(self) -> list[dict[str, Any]]:
        """按本地判定记录聚合出首页卡片内容块；读盘走 to_thread，失败即降级为静态描述。"""

        try:
            text = await asyncio.to_thread(self._read_records_text)
            summary = core.summarize_decision_records(core.parse_decision_records(text))
            breaker = self._breaker()
            now = asyncio.get_running_loop().time()
            # 只读判断：刻意不调用 breaker_should_skip()，避免渲染卡片时顺手改写熔断状态
            breaker_open = (
                breaker.opened_at is not None
                and now < breaker.opened_at + breaker.cooldown_seconds
            )
            remaining = 0.0
            if breaker_open and breaker.opened_at is not None:
                remaining = max(0.0, breaker.opened_at + breaker.cooldown_seconds - now)
            try:
                threshold = float(self.config.gate.probability_threshold or 0.80)
            except Exception:
                threshold = 0.80
            return core.build_home_card_blocks(
                summary,
                breaker_open=breaker_open,
                cooldown_remaining=remaining,
                breaker_hits=int(summary.get("breaker_skip") or 0),
                probability_threshold=threshold,
            )
        except Exception as exc:
            self.ctx.logger.warning("%s：首页卡片内容构造失败（已降级为静态描述）: %s", LOG_TAG, exc)
            return [dict(block) for block in _STATIC_CARD_CONTENT]

    @HomeCard(
        "jev_timing_gate",
        title="Jev 参与门控",
        description="门控成效：累计判定、抑制率、去重复用与熔断状态",
        content=_STATIC_CARD_CONTENT,
        link_url="/plugin-config?plugin=bg4jts.jev-timing-gate",
        link_label="打开插件配置",
        width="medium",
        order=200,
    )
    async def home_card(self) -> list[dict[str, Any]]:
        """首页卡片（被 ``@HomeCard`` 装饰的方法）：返回实时内容块。

        ``@HomeCard`` 的 ``content=`` 是注册期静态内容（见文档示例）；本方法额外返回
        实时聚合块。两种情形都只用文档支持的内容块类型，不含 HTML/JS。
        """

        return await self._home_card_blocks()

    # ------------------------------------------------------------------ Hook
    @HookHandler(
        "maisaka.planner.before_request",
        name="jev_timing_gate",
        description="用 Jev 判定本轮是否值得进入 planner；高置信无需参与时抑制该轮。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=HOOK_TIMEOUT_MS,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._maybe_gate(kwargs)
        except Exception as exc:  # 任何意外都必须放行，绝不能因为门控让机器人不吭声
            self.ctx.logger.warning("%s：异常（已忽略，正常进入 Planner）: %s", LOG_TAG, exc)
            return {"action": "continue"}


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""

    return JevTimingGatePlugin()
