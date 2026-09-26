"""gate_core 的离线单测（不需要 MaiBot SDK、不联网）。

直接跑：python tests/test_gate_core.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate_core import (  # noqa: E402
    CLASSIFIER_DEV_LABELS,
    DEFAULT_MENTION_WINDOW_CHARS,
    build_gate_skip_item,
    build_request,
    build_decision_record,
    evaluate_decision,
    extract_planner_state_text,
    is_bot_addressed,
    jev_extract_choice,
    jev_extract_probability,
    normalize_style,
    parse_response,
    resolve_auth,
    _is_message_content,
    BreakerState,
    DedupCache,
    breaker_record,
    breaker_should_skip,
    turn_fingerprint,
)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print("  %s %-56s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


# ---------------------------------------------------------------- 1. 状态文本提取
def test_extract():
    print("\n[1] extract_planner_state_text")
    items = [
        {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "系统提示，应被跳过"}]},
        {"item_type": "UserMessageItem", "parts": [
            {"type": "text", "text": '<message msg_id="1" time="10:00:00" user="甲">\n'},
            {"type": "text", "text": "你好"},
        ]},
        {"item_type": "UserMessageItem", "parts": [
            {"type": "text", "text": "【人物画像-内部参考】\n以下内容仅供内部推理"},
        ]},
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "时间：2026-09-22 10:00:00"}]},
        {"item_type": "ReasoningItem", "text_parts": ["模型自己的推理，无 parts 字段"]},
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "最近一句聊天"}]},
    ]
    state = extract_planner_state_text(items)
    check("保留聊天、丢弃系统/框架/时间", "你好" in state and "最近一句聊天" in state, True)
    check("丢弃 SystemMessageItem", "系统提示" in state, False)
    check("丢弃框架文本", "人物画像" in state, False)
    check("丢弃『时间：』开头", "时间：2026" in state, False)
    check("ReasoningItem（无 parts）不进 state", "模型自己的推理" in state, False)
    check("max_chars 截断取尾部", extract_planner_state_text(items, max_chars=5), state[-5:])


# ---------------------------------------------------------------- 2. @豁免窗口
def test_mention_window():
    print("\n[2] is_bot_addressed（只扫尾部窗口）")
    aliases = ["小助手"]
    filler = "闲聊" * 100                                     # 200 字符
    check("最新一条 @小助手🌸 → 命中",
          is_bot_addressed(filler + "\n@小助手🌸 在吗", aliases), True)
    check("群名片带后缀 @小助手Bot → 命中",
          is_bot_addressed(filler + "\n@小助手Bot 在吗", aliases), True)
    check("@幽幽子 → 不命中", is_bot_addressed(filler + "\n@幽幽子 /今日运势", aliases), False)
    check("邮箱 a@小助手.com → 不命中", is_bot_addressed(filler + "\n邮箱 a@小助手.com", aliases), False)
    check("@小助先生 → 不命中", is_bot_addressed(filler + "\n@小助先生 你好", aliases), False)

    # 核心回归：老 @ 在窗口之外时不应命中（否则门控会长期失效）
    stale = "@小助手🌸 想你了\n" + "闲聊" * 300 + "\n@幽幽子 /今日运势"
    check("历史 @ 远在窗口外 → 不命中（关键回归）", is_bot_addressed(stale, aliases), False)
    check("历史 @ 远在窗口外（扫全窗则命中）",
          is_bot_addressed(stale, aliases, 0), True)
    check("默认窗口 = 600", DEFAULT_MENTION_WINDOW_CHARS, 600)
    check("空文本 → 不命中", is_bot_addressed("", aliases), False)
    check("无别名配置 → 不命中", is_bot_addressed(filler + "\n@小助手🌸 在吗", []), False)


# ---------------------------------------------------------------- 3. 响应解析
def test_parse():
    print("\n[3] jev_extract_choice / jev_extract_probability")
    payload = {"answers": {"timing_gate": {
        "type": "choice", "choice": "no_reply", "confidence": 0.68,
        "probabilities": {"continue": 0.16, "no_reply": 0.84}}}}
    check("choice", jev_extract_choice(payload)[0], "no_reply")
    check("confidence", jev_extract_choice(payload)[1], 0.68)
    check("p(no_reply)", jev_extract_probability(payload, "no_reply"), 0.84)
    check("缺字段 → None", jev_extract_choice({"answers": {}}), (None, None))
    check("probabilities 缺失 → None", jev_extract_probability({"answers": {"timing_gate": {}}}, "no_reply"), None)


# ---------------------------------------------------------------- 4. 判定表
def test_decision():
    print("\n[4] evaluate_decision（阈值 0.80 / 回退 0.62）")
    P = {"probability_threshold": 0.80, "confidence_fallback_threshold": 0.62}

    d = evaluate_decision("no_reply", 0.68, {"no_reply": 0.84}, **P)
    check("p=0.84 → 抑制", d["suppress"], True)
    check("依据字段 = p_no_reply", d["evidence"], "p_no_reply")

    check("p=0.79（贴线以下）→ 放行", evaluate_decision("no_reply", 0.66, {"no_reply": 0.79}, **P)["suppress"], False)
    check("p=0.80（边界含）→ 抑制", evaluate_decision("no_reply", 0.66, {"no_reply": 0.80}, **P)["suppress"], True)
    check("直接点名那种 p=0.50 → 放行", evaluate_decision("no_reply", 0.62, {"no_reply": 0.50}, **P)["suppress"], False)
    check("choice=continue → 放行", evaluate_decision("continue", 0.95, {"no_reply": 0.05}, **P)["suppress"], False)
    check("调用失败 choice=None → 放行", evaluate_decision(None, None, {}, **P)["suppress"], False)
    check("调用失败 reason 标记", evaluate_decision(None, None, {}, **P)["reason"], "no_result")

    # 回退路径
    d2 = evaluate_decision("no_reply", 0.70, {}, **P)
    check("缺 probabilities + conf=0.70 → 抑制", d2["suppress"], True)
    check("回退依据字段 = conf", d2["evidence"], "conf")
    check("缺 probabilities + conf=0.55 → 放行", evaluate_decision("no_reply", 0.55, {}, **P)["suppress"], False)
    check("缺 probabilities + conf=None → 放行", evaluate_decision("no_reply", None, {}, **P)["suppress"], False)
    check("probabilities 非字典 → 回退", evaluate_decision("no_reply", 0.70, None, **P)["evidence"], "conf")


# ---------------------------------------------------------------- 5. 极简请求
def test_skip_item():
    print("\n[5] build_gate_skip_item")
    item = build_gate_skip_item()
    check("item_type", item["item_type"], "UserMessageItem")
    check("是 text part", item["parts"][0]["type"], "text")
    check("有 meta.item_id", bool(item["meta"]["item_id"]), True)
    check("自定义文本生效", build_gate_skip_item("（自定义）")["parts"][0]["text"], "（自定义）")
    check("默认文案前缀中性（无插件专有前缀）", item["parts"][0]["text"].startswith("【门控】"), True)


# ---------------------------------------------------------------- 6. 多提供商适配
def test_adapters():
    print()
    print("[6] 多提供商适配（请求构造 / 响应归一 / 鉴权归一）")
    text = "12:00:00[msg_id:1][甲]@小助手 在吗"

    check("未知风格退回 typesafe", normalize_style("whatever"), "typesafe")
    check("风格名大小写归一", normalize_style("CLASSIFIER_DEV"), "classifier_dev")

    check("typesafe 默认鉴权", resolve_auth("typesafe"), ("Authorization", "Bearer "))
    check("classifier_dev 无鉴权", resolve_auth("classifier_dev"), ("", ""))
    check("自定义鉴权头 + 无前缀", resolve_auth("typesafe", "x-api-key", "none"), ("x-api-key", ""))
    check("header=none 不发鉴权", resolve_auth("typesafe", "none"), ("", "Bearer "))

    body = build_request("typesafe", text=text, model="jev-1.13.0", bot_name="小助手")
    check("typesafe: 有 state", body.get("state"), text)
    check("typesafe: 有 questions", "timing_gate" in (body.get("questions") or {}), True)
    check("typesafe: 模型名透传", body.get("model"), "jev-1.13.0")
    check("提示词按昵称模板化（instruction）",
          "小助手" in body["questions"]["timing_gate"]["instructions"], True)
    check("提示词按昵称模板化（criteria）",
          "小助手" in body["questions"]["timing_gate"]["criteria"]["continue"], True)

    body2 = build_request("classifier_dev", text=text, bot_name="小助手")
    check("classifier_dev: inputs", body2.get("inputs"), [text])
    check("classifier_dev: labels", body2.get("labels"), list(CLASSIFIER_DEV_LABELS))
    check("classifier_dev: instructions 提到昵称", "小助手" in body2.get("instructions", ""), True)

    body3 = build_request("openai_json", text=text, model="jev-latest", bot_name="小助手")
    check("openai_json: 单条 user 消息", len(body3["messages"]), 1)
    check("openai_json: 正文含 JSON 要求", '"choice"' in body3["messages"][0]["content"], True)

    payload_ts = {"answers": {"timing_gate": {"choice": "no_reply", "confidence": 0.68,
                                             "probabilities": {"continue": 0.16, "no_reply": 0.84}}}}
    choice, conf, probs = parse_response("typesafe", payload_ts)
    check("typesafe 解析 choice", choice, "no_reply")
    check("typesafe 解析 probabilities", probs.get("no_reply"), 0.84)

    payload_cd_no = {"results": [{"label": CLASSIFIER_DEV_LABELS[1], "confidence": 0.9,
                                 "scores": {CLASSIFIER_DEV_LABELS[1]: 0.93}}]}
    choice, conf, probs = parse_response("classifier_dev", payload_cd_no)
    check("classifier_dev 解析 no_reply", choice, "no_reply")
    check("classifier_dev scores → probabilities", probs.get("no_reply"), 0.93)

    payload_cd_yes = {"results": [{"label": CLASSIFIER_DEV_LABELS[0], "confidence": 0.8}]}
    check("classifier_dev 解析 continue", parse_response("classifier_dev", payload_cd_yes)[0], "continue")
    check("classifier_dev 无 scores → 空概率", parse_response("classifier_dev", payload_cd_yes)[2], {})

    fence = "`" * 3
    fenced_json = fence + "json\n" + '{"choice":"no_reply","confidence":"0.82"}' + "\n" + fence
    payload_oj = {"choices": [{"message": {"content": fenced_json}}]}
    choice, conf, _ = parse_response("openai_json", payload_oj)
    check("openai_json 解析（含代码块/字符串数字）", (choice, conf), ("no_reply", 0.82))
    check("openai_json 容忍别名 yes/no",
          parse_response("openai_json", {"choices": [{"message": {"content": '{"choice":"yes"}'}}]})[0], "continue")
    check("openai_json 垃圾输出 → None",
          parse_response("openai_json", {"choices": [{"message": {"content": "我不知道"}}]}), (None, None, {}))

    # 归一后接判定：payload_cd_no 带 scores → probabilities["no_reply"] = 0.93（≥ 0.80），
    # 走 p_no_reply 主路径（evidence == "p_no_reply"），并非 confidence 回退。
    d = evaluate_decision(*parse_response("classifier_dev", payload_cd_no),
                          probability_threshold=0.80, confidence_fallback_threshold=0.62)
    check("classifier_dev 走回退阈值判定", (d["suppress"], d["evidence"]), (True, "p_no_reply"))


# ---------------------------------------------------------------- 7. allowlist 提取（todo 1）
def test_allowlist():
    print("\n[7] allowlist 优先提取（todo 1）")
    check("_is_message_content: <message 标签 → True",
          _is_message_content('<message user="甲">'), True)
    check("_is_message_content: 普通文本 → False",
          _is_message_content("普通聊天"), False)

    item_a = [{"item_type": "UserMessageItem", "parts": [
        {"type": "text", "text": '<message user="甲">\n'},
        {"type": "text", "text": "【人物画像-内部参考】"},
    ]}]
    state_a = extract_planner_state_text(item_a)
    check("含 <message> 的 Item 整条保留（含框架标记 part）",
          "<message" in state_a and "人物画像" in state_a, True)

    check("无 <message> 纯文本 → 回退仍提取",
          extract_planner_state_text([{"item_type": "UserMessageItem",
                                       "parts": [{"type": "text", "text": "最近一句聊天"}]}]),
          "最近一句聊天")

    check("纯框架文本（人物画像 + 时间）→ 空串",
          extract_planner_state_text([
              {"item_type": "UserMessageItem",
               "parts": [{"type": "text", "text": "【人物画像-内部参考】"}]},
              {"item_type": "UserMessageItem",
               "parts": [{"type": "text", "text": "时间：2026-09-22 10:00:00"}]},
          ]), "")

    both_items = [{"item_type": "UserMessageItem", "parts": [
        {"type": "text", "text": '<message user="甲">\n'},
        {"type": "text", "text": "你好"},
    ]},
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "最近一句聊天"}]}]
    state_both = extract_planner_state_text(both_items)
    check("标记 Item + 未标记 Item 都保留（test:53 不变量）",
          "你好" in state_both and "最近一句聊天" in state_both, True)


# ---------------------------------------------------------------- 8. 去重与熔断（todo 2/3）
def test_dedup_breaker():
    print("\n[8] 去重（DedupCache / turn_fingerprint）与熔断器（todo 2/3）")
    cache = DedupCache(capacity=8, ttl_seconds=100.0)
    cache.put("k", "v", 0.0)
    check("TTL 内命中", cache.get("k", 99.0), "v")
    check("TTL 边界（now >= expires_at）→ 过期", cache.get("k", 100.0), None)

    evict = DedupCache(capacity=8, ttl_seconds=100.0)
    for i in range(9):
        evict.put("k%d" % i, "v%d" % i, 0.0)
    check("9 键超容量 8 → 最早键被淘汰", evict.get("k0", 0.0), None)
    check("最新键仍在", evict.get("k8", 0.0), "v8")

    check("hint 不同但内容相同 → 同一指纹（保守去重）",
          turn_fingerprint("同一段内容", hint="a") == turn_fingerprint("同一段内容", hint="b"), True)

    s = BreakerState()
    for i in range(5):
        breaker_record(s, False, float(i))
    check("5 次连续失败 → 熔断打开", breaker_should_skip(s, 5.0), True)
    check("冷却期内 → 跳过", breaker_should_skip(s, 100.0), True)
    check("冷却到期 → 关闭并放行", breaker_should_skip(s, 305.0), False)
    check("关闭后 opened_at 复位", s.opened_at, None)

    s2 = BreakerState()
    for i in range(4):
        breaker_record(s2, False, float(i))
    breaker_record(s2, True, 4.0)
    breaker_record(s2, False, 5.0)
    check("4 失败 + 1 成功 + 1 失败 → 不打开", breaker_should_skip(s2, 6.0), False)


# ---------------------------------------------------------------- 9. 判定记录序列化（todo 10）
def test_decision_record():
    print("\n[9] build_decision_record（不含聊天正文的判定记录）")
    ts = "2026-09-26T12:00:00"

    d = {"suppress": True, "reason": "", "choice": "no_reply", "confidence": 0.90,
         "p_no_reply": 0.95, "evidence": "p_no_reply", "value": 0.95, "limit": 0.80}
    rec = build_decision_record(d, action="suppress", reused=False, breaker_skip=False, timestamp=ts)
    check("抑制记录：action=suppress", rec["action"], "suppress")
    check("抑制记录：ts 原样", rec["ts"], ts)
    check("抑制记录：reason 空串归一", rec["reason"], "")
    check("抑制记录：choice", rec["choice"], "no_reply")
    check("抑制记录：confidence", rec["confidence"], 0.90)
    check("抑制记录：p_no_reply", rec["p_no_reply"], 0.95)
    check("抑制记录：suppressed=True", rec["suppressed"], True)
    check("抑制记录：would_suppress=True", rec["would_suppress"], True)
    check("抑制记录：reused=False", rec["reused"], False)
    check("抑制记录：breaker_skip=False", rec["breaker_skip"], False)

    d2 = {"suppress": False, "reason": "no_result", "choice": None,
          "confidence": None, "p_no_reply": None}
    rec2 = build_decision_record(d2, action="continue", reused=False, breaker_skip=False, timestamp="t")
    check("no_result：action=continue", rec2["action"], "continue")
    check("no_result：reason=no_result", rec2["reason"], "no_result")
    check("no_result：choice=None", rec2["choice"], None)
    check("no_result：confidence=None", rec2["confidence"], None)
    check("no_result：p_no_reply=None", rec2["p_no_reply"], None)
    check("no_result：suppressed=False", rec2["suppressed"], False)
    check("no_result：would_suppress=False", rec2["would_suppress"], False)

    d3 = {"suppress": True, "choice": "no_reply", "confidence": 0.90, "p_no_reply": 0.95}
    rec3 = build_decision_record(d3, action="continue", reused=False, breaker_skip=False, timestamp="t")
    check("影子记录：action=continue（未实际抑制）", rec3["action"], "continue")
    check("影子记录：suppressed=False", rec3["suppressed"], False)
    check("影子记录：would_suppress=True（本该抑制）", rec3["would_suppress"], True)

    rec4 = build_decision_record({}, action="continue", reused=True, breaker_skip=True, timestamp="t")
    check("缺键：reason 降级为空串", rec4["reason"], "")
    check("缺键：choice=None", rec4["choice"], None)
    check("缺键：confidence=None", rec4["confidence"], None)
    check("缺键：p_no_reply=None", rec4["p_no_reply"], None)
    check("缺键：would_suppress=False", rec4["would_suppress"], False)
    check("缺键：suppressed=False", rec4["suppressed"], False)
    check("缺键：reused=True 透传", rec4["reused"], True)
    check("缺键：breaker_skip=True 透传", rec4["breaker_skip"], True)
    check("缺键：ts 透传", rec4["ts"], "t")

    chat_text = "最近一句聊天"
    rec5 = build_decision_record(
        {"suppress": True, "reason": "", "choice": "no_reply", "confidence": 0.90,
         "p_no_reply": 0.95, "chat_text": chat_text},
        action="suppress", reused=False, breaker_skip=False, timestamp="t")
    check("记录不含聊天正文（json.dumps 全串断言）",
          chat_text not in json.dumps(rec5, ensure_ascii=False), True)


if __name__ == "__main__":
    test_extract()
    test_mention_window()
    test_parse()
    test_decision()
    test_skip_item()
    test_adapters()
    test_allowlist()
    test_dedup_breaker()
    test_decision_record()
    print("\n通过 %d / 失败 %d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
