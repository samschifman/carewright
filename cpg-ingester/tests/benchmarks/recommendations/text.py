"""Presentation normalization that retains numbers, operators and clinical words."""

import re
import unicodedata

from prompt_eval.security import traced

_TOKEN = re.compile(
    r"(?:>=|<=|!=|>|<|=)?(?:\d+(?:\.\d+)?|\.\d+)|[^\W\d_]+(?:[/-][^\W\d_]+)*|[%/+−-]",
    re.UNICODE,
)
# SI prefixes are case sensitive: mIU and MIU differ by nine orders of magnitude.
_UNIT = re.compile(r"[fpnumkKMGTµμ]?(?:g|L|l|IU|U|mol|Eq|eq|m|s|Hz|Pa|A|V|W)$")


@traced
def tokens(text: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(
        str.maketrans({"≥": ">=", "≤": "<=", "≠": "!=", "–": "-", "—": "-", "−": "-"})
    )
    text = re.sub(r"([<>!]=?|=)\s+(?=\d)", r"\1", text)
    return tuple(
        "".join(
            part if _UNIT.fullmatch(part) else part.lower()
            for part in re.split(r"([/-])", token)
        )
        for token in _TOKEN.findall(text)
    )


def contains(text: str, phrase: str) -> bool:
    haystack, needle = tokens(text), tokens(phrase)
    return bool(needle) and any(
        haystack[i : i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )
