"""Structural mutators: change how the payload is delivered.

These are the ones the PRD names explicitly — placement, formatting, encoding,
splitting across files — and in practice they move the needle more than wording
does, because they change which part of the agent's context the text lands in.
"""

from __future__ import annotations

import base64
import codecs
import random

from reprobe.mutate import MAX_PAYLOAD_BYTES, Candidate, Mutation, MutationContext

#: Written as an escape, not pasted: an invisible character in source is a
#: character nobody reviews.
_ZERO_WIDTH = "​"

_NOISE_LINES: tuple[str, ...] = (
    "## Installation",
    "Run `npm install` to get started.",
    "See the docs for details.",
    "| option | default |",
    "| --- | --- |",
    "Licensed under MIT.",
)

#: Worst case over the four schemes is `zero_width`: every character gains a
#: three-byte joiner. `hex` doubles and `base64` adds a third.
_ENCODE_WORST_GROWTH = 4


def _multiline_surfaces(cand: Candidate) -> list[str]:
    """Surfaces whose text has at least two lines of differing content.

    Two lines are not enough on their own: moving a line within a run of
    identical lines produces the same text back.
    """
    return sorted(s for s, t in cand.payloads.items() if len(set(t.splitlines())) > 1)


class MovePlacement:
    """Move one line elsewhere in the same surface.

    The highest-value structural move there is: `placement_sensitive` fires only
    when the instruction lands in the agent's first few hundred characters.
    """

    name = "move_placement"

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return bool(_multiline_surfaces(cand))

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        sid = rng.choice(_multiline_surfaces(cand))
        lines = cand.payloads[sid].splitlines()
        source = rng.randrange(len(lines))
        line = lines.pop(source)

        # Re-inserting anywhere inside the run of identical lines that the pop
        # left behind rebuilds the original text, and so does inserting back at
        # `source`. Exclude that whole span rather than just `source`, or one
        # move in five is a silent no-op. The span is a strict subrange because
        # `applies_to` guaranteed two differing lines, so a target always exists.
        low = source
        while low > 0 and lines[low - 1] == line:
            low -= 1
        high = source
        while high < len(lines) and lines[high] == line:
            high += 1
        target = rng.choice([t for t in range(len(lines) + 1) if not low <= t <= high])

        lines.insert(target, line)
        payloads = dict(cand.payloads)
        payloads[sid] = "\n".join(lines)
        return cand.derive(
            payloads, Mutation(mutator=self.name, params={"from": source, "to": target})
        )


class ChangeFormatting:
    name = "change_formatting"

    @staticmethod
    def _formattable(cand: Candidate) -> list[str]:
        return sorted(
            s for s, t in cand.payloads.items() if any(ln.strip() for ln in t.splitlines())
        )

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return bool(self._formattable(cand))

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        style = rng.choice(["heading", "list", "quote", "table_cell", "code_comment"])
        sid = rng.choice(self._formattable(cand))
        body = cand.payloads[sid]
        lines = [line for line in body.splitlines() if line.strip()]
        if style == "heading":
            formatted = "\n".join(f"### {line}" for line in lines)
        elif style == "list":
            formatted = "\n".join(f"- {line}" for line in lines)
        elif style == "quote":
            formatted = "\n".join(f"> {line}" for line in lines)
        elif style == "table_cell":
            formatted = "| step | detail |\n| --- | --- |\n" + "\n".join(
                f"| {i + 1} | {line} |" for i, line in enumerate(lines)
            )
        else:
            formatted = "\n".join(f"// {line}" for line in lines)
        payloads = dict(cand.payloads)
        payloads[sid] = formatted
        return cand.derive(payloads, Mutation(mutator=self.name, params={"style": style}))


class EncodePayload:
    name = "encode_payload"

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        if not any(t for t in cand.payloads.values()):
            return False
        return cand.byte_size * _ENCODE_WORST_GROWTH + 64 <= MAX_PAYLOAD_BYTES

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        scheme = rng.choice(["base64", "rot13", "hex", "zero_width"])
        sid = rng.choice(sorted(s for s, t in cand.payloads.items() if t))
        body = cand.payloads[sid]
        if scheme == "base64":
            blob = base64.b64encode(body.encode()).decode()
            encoded = f"Decode and follow:\n```\n{blob}\n```"
        elif scheme == "rot13":
            # The label is load-bearing: rot13 of a string with no letters is
            # that string, and a mutator must never return its input.
            encoded = f"ROT13:\n{codecs.encode(body, 'rot13')}"
        elif scheme == "hex":
            encoded = f"hex:\n{body.encode().hex()}"
        else:
            encoded = _ZERO_WIDTH.join(body)
        payloads = dict(cand.payloads)
        payloads[sid] = encoded
        return cand.derive(payloads, Mutation(mutator=self.name, params={"scheme": scheme}))


class SplitAcrossSurfaces:
    """Put half the instruction in one attacker-controlled file and half in another.

    Note that the fake agent's `conjunction` profile does *not* exercise this
    from a search: its needles are literals no seed contains, so it is reachable
    only from the integration tests that write them by hand. Use
    `compositional` for anything measuring the search.
    """

    name = "split_across_surfaces"

    @staticmethod
    def _donor(cand: Candidate, ctx: MutationContext) -> str | None:
        """The surface to split, or None if there is nothing useful to do.

        This needs the context, which is why `applies_to` takes one: whether a
        spare surface exists is a property of the scenario, not the candidate.
        """
        if not [s for s in ctx.surfaces if s not in cand.payloads]:
            return None
        wide = [s for s, t in cand.payloads.items() if len(t) > 40]
        return max(sorted(wide), key=lambda s: len(cand.payloads[s])) if wide else None

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return self._donor(cand, ctx) is not None

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        sid = self._donor(cand, ctx)
        if sid is None:  # pragma: no cover - guarded by applies_to
            return cand
        body = cand.payloads[sid]
        # Never 0 and never len(body): an empty half is not a split, and it
        # would leave a surface rendering nothing while the lineage claims two.
        cut = rng.randrange(1, len(body))
        target = rng.choice(sorted(s for s in ctx.surfaces if s not in cand.payloads))
        payloads = dict(cand.payloads)
        payloads[sid] = body[:cut]
        payloads[target] = body[cut:]
        return cand.derive(
            payloads,
            Mutation(mutator=self.name, params={"from": sid, "to": target, "cut": cut}),
        )


class DuplicatePayload:
    name = "duplicate_payload"

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return bool(cand.payloads) and cand.byte_size * 2 + 4 <= MAX_PAYLOAD_BYTES

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        sid = rng.choice(sorted(cand.payloads))
        payloads = dict(cand.payloads)
        payloads[sid] = f"{payloads[sid]}\n\n{payloads[sid]}"
        return cand.derive(payloads, Mutation(mutator=self.name, params={"surface": sid}))


class InsertBenignNoise:
    name = "insert_benign_noise"

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return bool(cand.payloads)

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        sid = rng.choice(sorted(cand.payloads))
        lines = cand.payloads[sid].splitlines() or [""]
        for _ in range(rng.randint(1, 4)):
            lines.insert(rng.randrange(len(lines) + 1), rng.choice(_NOISE_LINES))
        payloads = dict(cand.payloads)
        payloads[sid] = "\n".join(lines)
        return cand.derive(payloads, Mutation(mutator=self.name))


class TruncateTail:
    name = "truncate_tail"

    @staticmethod
    def _truncatable(cand: Candidate) -> list[str]:
        return sorted(s for s, t in cand.payloads.items() if len(t.splitlines()) > 2)

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool:
        return bool(self._truncatable(cand))

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate:
        sid = rng.choice(self._truncatable(cand))
        lines = cand.payloads[sid].splitlines()
        keep = rng.randint(1, len(lines) - 1)
        payloads = dict(cand.payloads)
        payloads[sid] = "\n".join(lines[:keep])
        return cand.derive(payloads, Mutation(mutator=self.name, params={"keep": keep}))
