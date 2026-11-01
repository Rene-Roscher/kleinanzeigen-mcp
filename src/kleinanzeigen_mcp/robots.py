"""Minimal robots.txt matcher with Google semantics (``*`` wildcard, ``$`` anchor,
longest match wins, Allow wins ties). Python's ``urllib.robotparser`` ignores
wildcards, which kleinanzeigen's robots.txt relies on heavily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str, agent: str = "*") -> None:
        self.rules: list[_Rule] = []
        groups: list[tuple[list[str], list[_Rule]]] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if val:
                    rules.append(_Rule(key == "allow", val, _compile(val)))
        if agents or rules:
            groups.append((agents, rules))
        agent = agent.lower()
        chosen = [r for a, r in groups if agent in a] or [r for a, r in groups if "*" in a]
        for r in chosen:
            self.rules.extend(r)

    def allowed(self, path: str) -> bool:
        best: _Rule | None = None
        for r in self.rules:
            if r.regex.match(path) and (
                best is None
                or len(r.pattern) > len(best.pattern)
                or (len(r.pattern) == len(best.pattern) and r.allow)
            ):
                best = r
        return True if best is None else best.allow
