"""Turn a provider reasoning summary into a short, useful one-liner for the dashboard.

Gemini thought summaries look like ``**Invoice Review for Company X Ltd**\n\nOkay, here's my take on those
thoughts, tailored for an expert audience: Alright, let me see... I have the invoice list...``. The bold title
carries the information; the chatty preamble is boilerplate. :func:`clean_thought` keeps the title(s) plus the
first substantive sentence(s) and drops the filler.
"""
from __future__ import annotations

import re

THOUGHT_LIMIT = 200
_TITLE_LINE = re.compile(r"(?m)^[ \t]*\*\*([^*\n]+?)\*\*[ \t]*:?[ \t]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_ABBREVIATIONS = {"ltd.", "inc.", "co.", "corp.", "mr.", "mrs.", "ms.", "dr.", "no.", "vs.", "e.g.", "i.e.", "etc.", "st."}
_LOWERABLE = {"the", "a", "an", "it", "this", "that", "these", "those", "then", "next", "now", "we", "my", "there",
              "they", "he", "she", "its", "our", "their", "since", "so", "but", "and"}
_SHORT_SENTENCE = 50  # a first sentence this short gets the next one appended (when it fits)

# Boilerplate preambles, stripped (case-insensitively) from the start of the text, repeatedly.
_PREAMBLES = [re.compile(p, re.IGNORECASE) for p in (
    # interjections / fillers
    r"^(?:okay|ok|alright|all right|alrighty|right|so|well|now|wow|hmm+|um+|uh+|ah+|oh)\b[\s,.!:;…-]*",
    # "Here's my take / interpretation / summary / thought process ...:"
    r"^here(?:'|’)?s (?:my|the|a|an) (?:take|interpretation|summary|thought process|thoughts?|breakdown|analysis|"
    r"rundown|understanding|reading|reasoning)\b[^:.\n]*[:.]\s*",
    # "Here's your summary:" / "Here is a summary of ..." (also when it is the whole remaining sentence)
    r"^here(?:(?:'|’)s| is| are) (?:your|my|the|a|an|a quick|a short|a brief) (?:quick |short |brief )?"
    r"(?:summary|recap|overview)\b[^:.!…\n]*(?:[:.!…]+|$)\s*",
    # "Let's get down to business / brass tacks."
    r"^let(?:'|’)?s (?:get )?(?:right )?(?:down )?(?:to|into) (?:business|brass tacks)\b[\s,.!:;…-]*",
    # "Time to get into the details..."
    r"^(?:it(?:'|’)?s |it is )?time to (?:get|dive|jump|go) (?:right )?(?:down )?(?:in)?to\b[^:.!…\n]*(?:[:.!…]+|$)\s*",
    # "... tailored for an expert audience ...:"
    r"^[^:.\n]{0,100}\btailored (?:for|to) (?:an? |the )?(?:expert|experts|expert-level|technical|senior)\b[^:.\n]*[:.]\s*",
    # "... formatted as requested:"
    r"^[^:.\n]{0,120}\bformatted as requested\b[^:.\n]*[:.]\s*",
    # "Assuming I'm an expert ...:"
    r"^[^:.\n]{0,60}\bassuming (?:i(?:'|’)?m|i am|you(?:'|’)?re|you are) (?:an? )?(?:expert|experts)\b[^:.\n]*[:.]\s*",
    # "Let me see..." / "Let me think."
    r"^let(?:'|’)?s? (?:me )?(?:see|think|check|look|start|figure)\b[ ,]*(?:\.{2,}|…|[.!:])\s*",
    # stray ellipsis left over
    r"^(?:\.{2,}|…)\s*",
)]


def _strip_preambles(text: str) -> str:
    text = text.strip()
    while True:
        for pattern in _PREAMBLES:
            stripped = pattern.sub("", text, count=1)
            if stripped != text:
                text = stripped.lstrip()
                break
        else:
            return text


def _sentences(text: str) -> list[str]:
    """Split on sentence ends, but not after abbreviations like ``Ltd.``."""
    out: list[str] = []
    pieces = _SENTENCE_END.split(text)
    for piece in pieces:
        if out and out[-1].rsplit(" ", 1)[-1].lower() in _ABBREVIATIONS:
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return out


def _substantive(sentence: str) -> bool:
    words = re.findall(r"[\w'-]+", sentence)
    return len(sentence) >= 12 and len(words) >= 3


def _tidy(sentence: str) -> str:
    return sentence.strip().rstrip("…").rstrip(".").rstrip() if sentence.rstrip().endswith(("...", "…")) else sentence.strip()


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:→-–.") + "…"


def _clean_title(title: str) -> str:
    return " ".join(title.replace("**", "").split()).strip(" .:;")


def clean_thought(summary: str, limit: int = THOUGHT_LIMIT) -> str:
    """Condense a reasoning summary into ``"<Title> → <Title>: <first substantive sentence>"`` (<= ``limit`` chars).

    Without bold titles only the sentence is returned. Text that never matches the summary shape is returned
    whitespace-collapsed and shortened, so a non-empty input always gives a non-empty result.
    """
    raw = (summary or "").strip()
    if not raw:
        return ""
    parts = _TITLE_LINE.split(raw)  # [pre, title1, body1, title2, body2, ...]
    bodies = [parts[0]] + parts[2::2]
    titles: list[str] = []
    for title in parts[1::2]:
        title = _clean_title(title)
        if title and (not titles or titles[-1].lower() != title.lower()):
            titles.append(title)
    head = " → ".join(titles)

    sentences: list[str] = []
    for body in bodies:
        body = _strip_preambles(" ".join(body.replace("**", "").split()))
        for sentence in _sentences(body):
            sentence = _strip_preambles(sentence)
            if _substantive(sentence):
                sentences.append(sentence)
        if sentences:
            break

    room = limit - (len(head) + 2 if head else 0)
    text = ""
    if sentences and room >= 20:
        text = sentences[0]
        if len(_tidy(text)) < _SHORT_SENTENCE and len(sentences) > 1:
            second = sentences[1]
            first_word = second.split(" ", 1)[0].lower()
            if first_word in _LOWERABLE:
                second = second[0].lower() + second[1:]
            joined = f"{_tidy(text).rstrip('.')}; {second}"
            if len(joined) <= room:
                text = joined
    if head:
        combined = f"{head}: {text}" if text else head
    else:
        combined = text or " ".join(_strip_preambles(" ".join(raw.replace("**", "").split())).split()) or " ".join(raw.replace("**", "").split())
    return _shorten(combined, limit)
