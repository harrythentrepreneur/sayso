"""Hide personal data before it is stored or shown outside the live loop.

Used when real cases are copied into a console that more people can see.
Every customer becomes a stable pseudonym ("Customer 3F2A"); the same person
gets the same pseudonym everywhere, so a case still reads coherently.

What is hidden:
- email addresses (always; a customer's own address becomes their pseudonym)
- names we know belong to a customer (from their address, their "From:"
  display name, and "Name Surname - ..." case titles)
- school / organisation names ("Liberton Primary", "Ashburton College")
- phone numbers, card numbers, IP addresses
- payment-provider ids (cus_, ch_, sub_, pi_, in_, re_ ...)
- URL query strings (they often carry tokens)

Limits (say them, do not pretend): free-text names the loop has never seen
attached to a customer, and street addresses, are NOT reliably found. Review
a sample before sharing more widely.

The pseudonym is an HMAC of the address under a secret salt, so a list of
real addresses cannot be hashed to reverse it without the salt.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Iterable

EMAIL = re.compile(r"[A-Za-z0-9._%+'*•-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w/.-])\+?\(?\d[\d ()-]{7,}\d(?![\w/.-])")
CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PAYMENT_ID = re.compile(r"\b(?:cus|ch|sub|pi|py|in|re|cs|pm|txn|ba|card|seti|si|prod|price)_[A-Za-z0-9]{6,}\b")
URL_QUERY = re.compile(r"(https?://[^\s?#<>\"']+)\?[^\s<>\"']+")
ORG = re.compile(r"\b(?:[A-Z][\w'’.-]+\s){1,4}(?:School|College|Primary|Academy|High|District|Kindergarten|Kindy|"
                 r"Preschool|Elementary|Grammar|Montessori|University|Institute|Department of Education)\b")
DISPLAY_FROM = re.compile(r"^From:\s*(?P<name>[^<\n]+?)\s*<[^>\n]+>", re.M)
TITLE_NAME = re.compile(r"^\s*(?P<name>[A-Z][\w'’-]+(?:\s[A-Z][\w'’-]+){0,2})\s*(?:\(|-|—|–|\||,)")
HANDLE = re.compile(r"^\s*(?P<name>[\w.'’-]+(?:\s[\w.'’-]+){0,2})\s+[—–-]\s")   # "handle — book failed"
GREETING = re.compile(r"^(?:Hi|Hello|Hey|Dear|Thanks|Thank you),?\s+(?P<name>[A-Z][\w'’-]+(?:\s[A-Z][\w'’-]+)?)\s*[,!.]",
                      re.M)

# Words that look like first names but are also ordinary words; never masked alone.
_COMMON = frozenset("""may june april august will grace hope joy faith mark bill rose summer autumn page
ashley  the and for our you your case book books reply refund ticket school class teacher team support
sam dear hi hello thanks thank best kind regards new done customer""".split())


def _dictionary() -> frozenset[str]:
    """Lower-case English words (system word list if present). Names that are also words
    ("Brown", "Hall") are then masked only when capitalised, so "from" or "tree" stays readable."""
    words = set(_COMMON) | {"from", "to", "subject", "date", "unknown", "account", "events",
                            "learning", "marketing", "newsletter", "tutor", "branded", "admin", "info", "hello"}
    for path in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                words.update(w.strip() for w in fh if w.strip().islower())
            break
        except OSError:
            continue
    return frozenset(words)


_WORDS = _dictionary()


def _proper_nouns() -> frozenset[str]:
    """Capitalised entries of the system word list: mostly first names, surnames and places."""
    out = set()
    for path in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                out.update(w.strip() for w in fh if w[:1].isupper() and w.strip().isalpha() and len(w.strip()) >= 3)
            break
        except OSError:
            continue
    return frozenset(out)


# Proper nouns that are safe and useful to keep readable.
KEEP = frozenset("""January February March April May June July August September October November December
Monday Tuesday Wednesday Thursday Friday Saturday Sunday Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec Mon Tue Wed Thu Fri Sat Sun English Spanish French German Norwegian Maori Māori
Australia Australian America American Britain British England Canada Zealand Europe European Google Stripe
Discord Christmas Easter Word PDF Yes No""".split())
_NEVER = frozenset({"from", "to", "subject", "date", "unknown", "customer", "account", "re", "fwd"})


def _name_parts(text: str) -> list[str]:
    return [p for p in re.split(r"[\s._+'’-]+|\d+", text) if len(p) >= 3 and p.isalpha()]


class Masker:
    def __init__(self, salt: str, extra_names: Iterable[str] = (), *, strict: bool = False,
                 keep: Iterable[str] = ()):
        """keep: proper nouns to leave readable in strict mode (your product, team and character names)."""
        if not salt or len(salt) < 16:
            raise ValueError("privacy salt must be a secret of at least 16 characters")
        self._salt = salt.encode()
        self._names: dict[str, str] = {}   # lower-case name part -> replacement
        self._rx: tuple | None = None
        self.strict = strict
        self._proper = None
        if strict:  # also hide ANY capitalised proper noun from the word list (first names in lists, places)
            nouns = sorted(_proper_nouns() - KEEP - frozenset(keep), key=len, reverse=True)
            self._proper = re.compile(r"\b(" + "|".join(map(re.escape, nouns)) + r")\b") if nouns else None
        for n in extra_names:
            self.learn_name(n, "[name]")

    # -- identities ---------------------------------------------------------
    def pseudonym(self, email: str) -> str:
        digest = hmac.new(self._salt, email.strip().lower().encode(), hashlib.sha256).hexdigest()
        return f"Customer {digest[:4].upper()}"

    def learn_name(self, name: str, replacement: str = "[name]") -> None:
        whole = re.sub(r"\s+", " ", name.strip())
        if len(whole) >= 3 and whole.lower() not in _NEVER and whole.lower() not in _COMMON and not whole.isdigit():
            self._names.setdefault(whole.lower(), replacement)   # handles like "sunnyreader42"
        for part in _name_parts(name):
            if part.lower() not in _COMMON and part.lower() not in _NEVER:
                self._names.setdefault(part.lower(), replacement)
        self._rx = None

    def learn_customer(self, email: str, *texts: str) -> str:
        """Register a customer: their address, and names seen next to them."""
        who = self.pseudonym(email)
        local = email.split("@")[0]
        self.learn_name(local, who)
        for t in texts:
            for m in DISPLAY_FROM.finditer(t or ""):
                self.learn_name(m.group("name"), who)
            for rx in (TITLE_NAME, HANDLE):
                m = rx.match(t or "")
                if m:
                    self.learn_name(m.group("name"), who)
            for m in GREETING.finditer(t or ""):
                self.learn_name(m.group("name"), who)
        return who

    # -- text -----------------------------------------------------------------
    def text(self, value) -> str:
        if value is None:
            return ""
        s = str(value)
        s = URL_QUERY.sub(r"\1?…", s)
        s = EMAIL.sub(lambda m: self.pseudonym(m.group(0)), s)
        s = PAYMENT_ID.sub(lambda m: m.group(0).split("_")[0] + "_•••", s)
        s = CARD.sub("[card number]", s)
        s = IP.sub("[ip]", s)
        s = PHONE.sub("[phone]", s)
        s = ORG.sub("[school]", s)
        if self._names:
            s = self.mask_names(s)
        if self._proper is not None:
            s = self._proper.sub("[name]", s)
            s = re.sub(r"\[name\](?:[ ,]+(?:and )?\[name\])+", "[names]", s)
            s = re.sub(r"(Customer [0-9A-F]{4}|\[name\])(?:[\s.]+\1)+", r"\1", s)  # "Pat Lee" -> one pseudonym
        return s

    def _patterns(self):
        if self._rx is None:
            rare = sorted((n for n in self._names if n not in _WORDS), key=len, reverse=True)
            word = sorted((n for n in self._names if n in _WORDS), key=len, reverse=True)
            self._rx = (re.compile(r"\b(" + "|".join(map(re.escape, rare)) + r")\b", re.I) if rare else None,
                        re.compile(r"\b(" + "|".join(re.escape(w.capitalize()) for w in word) + r")\b")
                        if word else None)
        return self._rx

    def mask_names(self, s: str) -> str:
        """Known names only. Rare names in any case; names that are also words only when capitalised."""
        for rx in self._patterns():
            if rx is not None:
                s = rx.sub(lambda m: self._names[m.group(0).lower()], s)
        return s

    def learn_label(self, label: str) -> None:
        """A human-written label such as "Pat Lee - full USD 19 refund"."""
        m = TITLE_NAME.match(label or "")
        if m and not m.group("name").lower().startswith(("pr ", "money ", "customer ", "close ", "refund")):
            self.learn_name(m.group("name"), "[name]")

    def deep(self, value):
        """Mask every string inside a JSON-like value."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        return value


def leaks(text: str) -> list[str]:
    """What still looks like PII after masking. Used by tests and the importer's final check."""
    found = []
    for name, rx in (("email", EMAIL), ("payment id", PAYMENT_ID), ("card", CARD), ("ip", IP)):
        found += [f"{name}: {m.group(0)[:3]}…" for m in rx.finditer(text)]
    return found
