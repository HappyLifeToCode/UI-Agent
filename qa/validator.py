"""训练样本结构校验器
用法:
    from qa.validator import validate_sample
    issues = validate_sample(sample)   # 空列表 = 通过
"""
from dataclasses import dataclass
import re
@dataclass
class Issue:
    rule: str      # 规则名,如 "tool_pairing"
    level: str     # "error" | "warning"
    location: str  # 位置,如 "messages[5]"
    message: str   # 人话描述,写清为什么不合格
REQUIRED_META = ["task_id", "session_id", "agent", "source", "sample_index"]
VALID_ROLES = {"system", "user", "assistant", "tool"}
def check_meta(meta, issues):
    """规则1:meta 必填字段完整"""
    for key in REQUIRED_META:
        if key not in meta or meta[key] is None:
            issues.append(Issue("meta_required", "error", "meta",
                                f"缺少必填字段 meta.{key}"))
def check_roles(messages, issues):
    """规则2:角色合法、序列合理"""
    if not messages:
        issues.append(Issue("messages_empty", "error", "messages", "messages 为空"))
        return
    if messages[0].get("role") != "system":
        issues.append(Issue("role_sequence", "warning", "messages[0]",
                            "首条消息不是 system"))
    if not any(m.get("role") == "user" for m in messages):
        issues.append(Issue("role_sequence", "error", "messages",
                            "缺少 user 消息(任务指令)"))
    for i, msg in enumerate(messages):
        if msg.get("role") not in VALID_ROLES:
            issues.append(Issue("role_valid", "error", f"messages[{i}]",
                                f"非法 role: {msg.get('role')}"))
def check_tool_pairing(messages, issues):
    """规则3(核心):tool_call 与 tool 消息配对且相邻"""
    seen_ids = set()
    i = 0
    while i < len(messages):
        msg = messages[i]
        # 情况A:带 tool_calls 的 assistant → 后面必须紧跟等量 tool 消息
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            call_ids = [c.get("id") for c in msg["tool_calls"]]
            for j, cid in enumerate(call_ids):
                if not cid:
                    issues.append(Issue("tool_call_id", "error",
                                        f"messages[{i}].tool_calls[{j}]", "tool_call 缺少 id"))
                elif cid in seen_ids:
                    issues.append(Issue("tool_call_id", "error",
                                        f"messages[{i}].tool_calls[{j}]", f"id 重复: {cid}"))
                seen_ids.add(cid)
            for k, cid in enumerate(call_ids):
                pos = i + 1 + k
                if pos >= len(messages):
                    issues.append(Issue("tool_pairing", "error", f"messages[{i}]",
                                        f"tool_call {cid} 后缺少对应 tool 消息(断档)"))
                    break
                tmsg = messages[pos]
                if tmsg.get("role") != "tool":
                    issues.append(Issue("tool_pairing", "error", f"messages[{pos}]",
                                        f"tool_call {cid} 后应为 tool 消息,实际是 {tmsg.get('role')}"))
                    break
                if tmsg.get("tool_call_id") != cid:
                    issues.append(Issue("tool_pairing", "error", f"messages[{pos}]",
                                        f"tool_call_id={tmsg.get('tool_call_id')} 与期望的 {cid} 不匹配"))
            i += 1 + len(call_ids)
        # 情况B:孤立的 tool 消息(前面没有带 tool_calls 的 assistant)
        elif msg.get("role") == "tool":
            issues.append(Issue("tool_orphan", "error", f"messages[{i}]",
                                "孤立 tool 消息:前面没有带 tool_calls 的 assistant"))
            i += 1
        else:
            i += 1
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "疑似 API key"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "疑似 Bearer token"),
]
def check_secrets(messages, issues):
    """规则4:脱敏复检,样本里不允许出现密钥"""
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if isinstance(content, str):
            for pat, desc in SECRET_PATTERNS:
                if pat.search(content):
                    issues.append(Issue("secret_leak", "error", f"messages[{i}]",
                                        f"{desc},需脱敏后重新入库"))
def validate_sample(sample: dict) -> list:
    """主入口:校验一条样本,返回 Issue 列表(空 = 通过)"""
    issues = []
    check_meta(sample.get("meta", {}), issues)
    messages = sample.get("messages", [])
    check_roles(messages, issues)
    check_tool_pairing(messages, issues)
    check_secrets(messages, issues)
    return issues