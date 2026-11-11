"""R4 memo scanner: instruction-like text inside untrusted memos, and Pix keys or account numbers used in one.

HW7 change (evidence: benchmark tuning cases T12 and T13, see docs/hw7/): urgency words alone and a key, tax
id or contact printed without any payment instruction no longer count. They held legitimate invoices, while
the destination is already enforced by R3 and execute_payment pays only the registered key.
"""

from __future__ import annotations

import re
import unicodedata

# Normalized (lowercase, no accents). Spaces match any whitespace run.
PHRASES = (
    "nova chave",
    "chave nova",
    "novos dados bancarios",
    "mudamos de banco",
    "mudanca de banco",
    "alteramos os dados bancarios",
    "alteramos a conta",
    "nova conta",
    "pague para",
    "pague na",
    "transfira para",
    "deposite na",
    "desconsidere",
    "ignore as instrucoes",
    "ignore todas as instrucoes",
    "new key",
    "new account",
    "changed bank",
    "changed our bank",
    "updated bank details",
    "new bank details",
    "pay to",
    "send to",
    "transfer to",
    "ignore previous",
    "ignore all previous",
    "ignore the previous",
    "disregard",
    "override",
)

# A key or account number in the memo counts only next to one of these payment verbs (or a phrase above).
PAYMENT_VERBS = (
    "pague",
    "pagar",
    "transfira",
    "transferir",
    "deposite",
    "depositar",
    "envie",
    "enviar",
    "pay",
    "transfer",
    "send",
    "deposit",
    "remit",
)


def _words_re(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(r"\b" + r"\s+".join(map(re.escape, p.split())) + r"\b" for p in words))


_PHRASE_RE = _words_re(PHRASES)
_VERB_RE = _words_re(PAYMENT_VERBS)

_KEY_PATTERNS = (
    re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+"),  # email
    re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"),  # CPF formatted
    re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)"),  # CNPJ formatted
    re.compile(r"(?<!\d)\d{14}(?!\d)"),  # CNPJ digits
    re.compile(r"(?<!\d)\d{11}(?!\d)"),  # CPF digits
    re.compile(r"\+55[\s\-]?\(?\d{2}\)?[\s\-]?9?\d{4}[\s\-]?\d{4}"),  # phone
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),  # EVP
    re.compile(r"\b(?:agencia|ag|conta|cc|account|agency)\b\.?\s*(?:n|no|num|numero|number)?\.?\s*[:#]?\s*\d{3,}"),
)


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower()


def find_markers(memo: str) -> list[str]:
    """Return the matched marker texts (empty list when the memo is clean)."""
    text = normalize(memo)
    hits = [m.group(0) for m in _PHRASE_RE.finditer(text)]
    keys = [m.group(0) for pattern in _KEY_PATTERNS for m in pattern.finditer(text)]
    if hits or _VERB_RE.search(text):
        hits.extend(keys)
    return hits
