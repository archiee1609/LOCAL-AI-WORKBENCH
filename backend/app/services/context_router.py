"""
Conversation Topic & Relevance Router.
Prevents prior search dumps (hardware specs, KB tips, SOPs, web search) from bleeding
into unrelated follow-up requests (such as drafting an email in a different context),
while retaining relevant prior turns when the user asks a follow-up or revisits an earlier topic.
"""

import re
from typing import Any, Dict, List, Set

_FOLLOWUP_BACKREF_RE = re.compile(
    r"\b(?:"
    r"based\s+on\s+(?:this|that|these|those|the\s+above|previous|earlier|your\s+(?:previous|last|above))|"
    r"from\s+(?:the\s+)?(?:above|previous|earlier|last\s+(?:message|search|response|specs?|tips?))|"
    r"(?:this|that|same|above|previous)\s+(?:model|laptop|desktop|device|machine|mtm|issue|tip|sop|policy|spec|specs|configuration|result|search|error|problem|email|mail|draft)|"
    r"the\s+one\s+(?:we|i)\s+(?:searched|checked|looked\s+up|discussed)|"
    r"(?:we|i)\s+(?:checked|searched|discussed|looked\s+up)\s+(?:earlier|before|above|previously)|"
    r"its\s+(?:ram|cpu|processor|battery|display|screen|ports|storage|ssd|graphics|gpu|weight|warranty|specs?|price)|"
    r"compare\s+(?:both|them|these|those|the\s+two)|"
    r"(?:explain|elaborate|expand|summarize|rewrite|shorten|rephrase|translate|convert|polish)\s+(?:this|that|it|the\s+above)|"
    r"make\s+(?:it|this|that)\s+(?:more\s+)?(?:formal|polite|concise|shorter|longer|detailed|professional|friendly|brief)|"
    r"(?:also|please)\s+(?:add|include|mention)\b"
    r")\b",
    re.I,
)

_SHORT_ELLIPTICAL_RE = re.compile(
    r"^(?:what\s+about|how\s+about|and\s+(?:what|how|its|the)|does\s+it|can\s+it|is\s+it|why\s+(?:does|is|not)|which\s+one|tell\s+me\s+more)\b",
    re.I,
)

_COMPARE_RE = re.compile(r"\b(?:compare|comparison|versus|vs\.?|difference\s+between)\b", re.I)

_LENOVO_MODEL_RE = re.compile(
    r"\b(?:thinkpad|ideapad|thinkbook|thinkcentre|thinkstation|legion|yoga|loq|v14|v15|x1\s+carbon)\b",
    re.I,
)
_ALNUM_CODE_RE = re.compile(r"\b([A-Z0-9]{4,12})\b", re.I)

_NOISE_TOKENS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can", "could", "did", "do", "does",
    "draft", "email", "explain", "find", "for", "from", "get", "give", "good", "have", "hello", "help",
    "hi", "how", "i", "in", "into", "is", "it", "its", "just", "know", "letter", "like", "look", "mail",
    "make", "me", "message", "more", "my", "need", "new", "no", "not", "now", "of", "on", "one", "or",
    "our", "out", "please", "prepare", "reply", "respond", "response", "say", "search", "send", "show",
    "some", "summarize", "summary", "tell", "thank", "thanks", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "to", "update", "us", "use", "want", "was", "we", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "write", "you", "your", "customer", "client",
    "manager", "team", "user", "sir", "madam", "dear", "regards", "subject",
}

_CITATION_TRAILER_RE = re.compile(
    r"\n+---\n\*\*(?:📚 Lenovo Tips & Resources Citations|📜 SOP & Policy Citations|📋 Official PSREF Source|🌐 Verified Web Sources).*$",
    re.DOTALL,
)


def _clean_history_text(text: str) -> str:
    cleaned = _CITATION_TRAILER_RE.sub("", text or "")
    cleaned = re.sub(r"\n*<!--\s*kb_video_meta:\{.*?\}\s*-->", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _extract_hardware_codes(text: str) -> Set[str]:
    codes: Set[str] = set()
    scan = re.sub(r"https?://\S+", " ", text or "")
    for m in _ALNUM_CODE_RE.finditer(scan):
        tok = m.group(1).upper()
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            if not re.match(r"^(?:USB|DDR|GEN|PCI|WIN|BT\d|DP\d|HDMI|SATA|\d+(?:GB|MB|TB|HZ|MM|WH|PX|W))$", tok):
                codes.add(tok)
    return codes


def _extract_topic_tokens(text: str) -> Set[str]:
    clean = re.sub(r"https?://\S+", " ", (text or "").lower())
    raw = re.findall(r"[a-z0-9]{3,}", clean)
    return {t for t in raw if t not in _NOISE_TOKENS}


def _extract_model_families(text: str) -> Set[str]:
    return {m.group(0).lower().replace("  ", " ") for m in _LENOVO_MODEL_RE.finditer(text or "")}


def _pair_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Groups sequential messages into logical turns ([user, assistant?] or standalone message)."""
    turns: List[List[Dict[str, Any]]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user":
            pair = [msg]
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                pair.append(messages[i + 1])
                i += 2
            else:
                i += 1
            turns.append(pair)
        else:
            turns.append([msg])
            i += 1
    return turns


def select_relevant_chat_history(
    current_prompt: str,
    prior_messages: List[Dict[str, Any]],
    max_turns: int = 5,
    has_fresh_context: bool = False,
) -> Dict[str, Any]:
    """
    Selects only the prior conversation turns relevant to `current_prompt` (ChatGPT/Gemini-style
    topic switching inside a single thread).

    Returns:
      {
        "messages": List[Dict[str, Any]],  # Cleaned prior messages to include before current user prompt
        "topic_shifted": bool,             # True when unrelated prior turns were excluded
        "excluded_turns": int,
      }
    """
    if not prior_messages:
        return {"messages": [], "topic_shifted": False, "excluded_turns": 0}

    prompt = (current_prompt or "").strip()
    turns = _pair_turns(prior_messages)
    if not turns:
        return {"messages": [], "topic_shifted": False, "excluded_turns": 0}

    is_explicit_followup = bool(_FOLLOWUP_BACKREF_RE.search(prompt)) or (
        len(prompt.split()) <= 8 and bool(_SHORT_ELLIPTICAL_RE.search(prompt))
    )
    is_compare = bool(_COMPARE_RE.search(prompt))
    curr_codes = _extract_hardware_codes(prompt)
    curr_models = _extract_model_families(prompt)
    curr_tokens = _extract_topic_tokens(prompt)

    selected_turns: List[List[Dict[str, Any]]] = []
    excluded = 0

    for idx, turn in enumerate(turns):
        is_latest_turn = idx == len(turns) - 1
        combined_turn_text = "\n".join(str(m.get("content") or "") for m in turn)
        turn_codes = _extract_hardware_codes(combined_turn_text)
        turn_models = _extract_model_families(combined_turn_text)
        turn_tokens = _extract_topic_tokens(combined_turn_text)

        has_code_overlap = bool(curr_codes & turn_codes) or any(
            (len(c1) >= 4 and len(c2) >= 4 and (c1[:4] == c2[:4]))
            for c1 in curr_codes
            for c2 in turn_codes
        )
        has_model_overlap = bool(curr_models & turn_models)

        # 1. Explicit follow-up without overriding named model/code (e.g. "Make it formal", "Based on that, draft a mail")
        if is_explicit_followup and is_latest_turn and not (curr_codes or curr_models):
            selected_turns.append(turn)
            continue

        # 2. If current prompt specifies a model/MT/MTM and turn has a different model/MT/MTM:
        # exclude unless comparing
        if (curr_codes or curr_models) and (turn_codes or turn_models) and not (has_code_overlap or has_model_overlap):
            if is_compare and (is_explicit_followup or is_latest_turn):
                selected_turns.append(turn)
            else:
                excluded += 1
            continue

        # 3. Direct entity match (user queries or revisits an earlier searched model/MT/MTM)
        if has_code_overlap or has_model_overlap:
            if is_explicit_followup or not has_fresh_context or is_latest_turn:
                selected_turns.append(turn)
            else:
                excluded += 1
            continue

        # If current prompt targets a specific model/MTM and this turn does NOT mention it, exclude it
        if (curr_codes or curr_models) and not (is_compare and is_latest_turn):
            excluded += 1
            continue

        # If prior turn was anchored on a specific hardware model/MT/MTM and current prompt
        # does NOT reference that model and is NOT an explicit follow-up, isolate it cleanly
        if (turn_codes or turn_models) and not is_explicit_followup:
            excluded += 1
            continue

        # 4. General topic token overlap check (for non-hardware-specific discussions / drafts)
        overlap_tokens = curr_tokens & turn_tokens
        min_overlap = 1 if is_explicit_followup else 2
        if len(overlap_tokens) >= min_overlap and not has_fresh_context:
            selected_turns.append(turn)
            continue

        excluded += 1

    # Cap to most recent relevant turns & sanitize bloated citation trailers
    kept_turns = selected_turns[-max_turns:]
    cleaned_msgs: List[Dict[str, Any]] = []
    for turn in kept_turns:
        for m in turn:
            item = dict(m)
            content_str = str(item.get("content") or "")
            item["content"] = _clean_history_text(content_str)
            # Keep compact cap on very large prior assistant spec/KB dumps when still included
            if item.get("role") == "assistant" and len(item["content"]) > 3500:
                item["content"] = item["content"][:3500] + "\n...[truncated prior output]"
            cleaned_msgs.append(item)

    return {
        "messages": cleaned_msgs,
        "topic_shifted": excluded > 0,
        "excluded_turns": excluded,
    }


def build_context_scope_note(topic_shifted: bool) -> str:
    """Returns a concise instruction preventing bleed from earlier unrelated topics."""
    if not topic_shifted:
        return ""
    return (
        "### Current Request Scope:\n"
        "The user switched to a new or independent topic. Respond strictly to the CURRENT user request "
        "using only context provided for this request. Do NOT carry over models, part numbers, or details from unrelated earlier searches."
    )
