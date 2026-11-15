"""The kit's seeded scenario, ported exactly from program/env/src/wire.ts.

The suite's harness builds its world from one fixed seed
(SCENARIO_SEED 0x6a09e667f3bcc909); the token owner is the scenario's
first user. The forge must know that login, because every repository
belongs to it and `viewer { login }` must answer it. The generator is
reproduced bit-for-bit here (FNV-1a-64 + splitmix-style mix + xorshift64
with the 0x2545f4914f6cdd1d multiplier) so the forge derives the same
viewer without ever seeing the scenario.

Verified against the kit: viewer adaiversen, teammate quinnkeller,
stranger olgabauer.
"""

from __future__ import annotations

_MASK64 = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909
SCENARIO_SEED_HEX = "6a09e667f3bcc909"
SCENARIO_EPOCH_SEC = 1735689600  # 2025-01-01T00:00:00Z, the fixture epoch

FIRST_NAMES = (
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo",
    "iris", "jonas", "kira", "lars", "mira", "nico", "olga", "pavel",
    "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara",
    "zane",
)
LAST_NAMES = (
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia",
    "holm", "iversen", "jansen", "keller", "lind", "moreau", "novak",
    "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich",
    "vega", "weber", "xu", "yilmaz", "zimmer",
)


def fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _MASK64
    return h


def mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (z ^ (z >> 31)) & _MASK64


def scenario_seed_for(label: str) -> int:
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    """xorshift64* RNG, byte-identical to wire.ts's Rng."""

    __slots__ = ("_state",)

    def __init__(self, seed: int):
        masked = seed & _MASK64
        self._state = masked if masked != 0 else SCENARIO_SEED

    @classmethod
    def from_label(cls, label: str) -> "Rng":
        return cls(scenario_seed_for(label))

    def clone(self) -> "Rng":
        return Rng(self._state)

    def next_u64(self) -> int:
        x = self._state
        x = (x ^ (x >> 12)) & _MASK64
        x = (x ^ ((x << 25) & _MASK64)) & _MASK64
        x = (x ^ (x >> 27)) & _MASK64
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & _MASK64

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, max_exclusive: int) -> int:
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def fraction(self) -> float:
        return self.next_u32() / 4294967296

    def chance(self, probability: float) -> bool:
        return self.fraction() < probability

    def pick(self, items):
        return items[self.next_int(len(items))]


def generate_users(rng: Rng, count: int):
    used = set()
    users = []
    for i in range(max(0, count)):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = first + last
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid",
        })
    return users


_USERS = generate_users(Rng.from_label("users"), 12)
VIEWER = _USERS[0]
TEAMMATE = _USERS[1]
STRANGER = _USERS[2]
VIEWER_LOGIN = VIEWER["login"]
VIEWER_ID = VIEWER["id"]
VIEWER_NAME = VIEWER["name"]
VIEWER_EMAIL = VIEWER["email"]
STRANGER_LOGIN = STRANGER["login"]
