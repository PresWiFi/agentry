from __future__ import annotations

import hashlib
import html
import json
import math
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    p = _Text()
    p.feed(s)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + " …[truncated]"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:24]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors)]


def parse_json(raw: str) -> dict:
    """Pulls the first JSON object out of a model reply (tolerates code fences and chatter)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        v = json.loads(raw)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(raw[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        break
                    break
        start = raw.find("{", start + 1)
    raise ValueError("No JSON object in the model's reply")


_ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "h2", "h3", "h4", "pre", "code",
                 "table", "thead", "tbody", "tr", "th", "td", "a", "img", "blockquote", "hr", "span", "div"}


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip += 1
            return
        if self.skip or tag not in _ALLOWED_TAGS:
            return
        keep = []
        for k, v in attrs:
            v = v or ""
            if tag == "a" and k == "href" and re.match(r"^(https?:|mailto:|/)", v.strip(), re.I):
                keep.append(f'{k}="{html.escape(v, quote=True)}"')
            elif tag == "img" and k in ("src", "alt") and (k == "alt" or re.match(r"^(https?:|/)", v.strip(), re.I)):
                keep.append(f'{k}="{html.escape(v, quote=True)}"')
        self.out.append(f"<{tag}{' ' + ' '.join(keep) if keep else ''}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in _ALLOWED_TAGS and tag not in ("br", "img", "hr"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def clean_html(s: str) -> str:
    """Keeps formatting tags only. Model output goes through this before it's stored or sent anywhere."""
    p = _Clean()
    p.feed(s or "")
    p.close()
    return "".join(p.out).strip()
