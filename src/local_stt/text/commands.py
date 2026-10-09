"""Built-in spoken commands, `text.commands` (docs/08-text-injection.md §8.2 step 4a, task 5.1).

The engine writes a command as ordinary words with its own punctuation around them
("Uwaga, dwukropek. Jutro"), so a match also takes the spaces and punctuation on both sides.
"""

import re

# Spoken form (lowercase, words separated by one space) -> inserted text. Only signs that the
# engine does not set from pauses, and only spoken forms that Parakeet wrote in the 2026-10-08/09
# recordings (user decision 2026-10-09: no "przecinek", "kropka", "znak zapytania").
DASH = "\u2013"  # półpauza, with a space on both sides

COMMANDS: dict[str, str] = {
    "dwukropek": ":",
    "dwóch kropek": ":",  # Parakeet's form of "dwukropek" in 2 of 5 takes
    "średnik": ";",
    "myślnik": DASH,
    "trzy kropki": "...",
    "3 kropki": "...",  # Parakeet writes the number as a digit in some takes
    "nowa linia": "\n",
    "new line": "\n",
}

_SURROUNDING = r"[\s,.;:!?…\u2013\u2014-]*"
_SENTENCE_END = ".?!…"
_ALTERNATIVES = "|".join(
    r"\s+".join(map(re.escape, spoken.split()))
    for spoken in sorted(COMMANDS, key=len, reverse=True)
)
_SPACE_BEFORE_NEWLINE = re.compile(r" +\n")
_COMMAND = re.compile(
    rf"(?P<before>{_SURROUNDING})\b(?P<spoken>{_ALTERNATIVES})\b(?P<after>{_SURROUNDING})",
    re.IGNORECASE,
)


def apply_commands(text: str) -> str:
    """Step 4a: spoken commands -> punctuation and line breaks.

    A sign follows the previous word without a space; a dash has a space on both sides. After
    ":", ";" and the dash, the next word starts lowercase when the engine ended a sentence after
    the command; after "..." it starts uppercase. A line break keeps the sentence end the
    engine put before it, has no spaces around it, and does not change the next word. Signs
    from commands in a row are joined without spaces ("trzy kropki dwukropek" -> "...:").
    """
    parts: list[str] = []
    case: str | None = None  # "lower" | "upper" for the first letter of the next text
    pos = 0
    for m in _COMMAND.finditer(text):
        chunk = text[pos : m.start()]
        symbol = COMMANDS[" ".join(m["spoken"].lower().split())]
        if not chunk and parts and symbol not in ("\n", DASH):
            parts[-1] = parts[-1].rstrip(" ")  # the previous command's sign
        parts.append(_change_case(chunk, case))
        if symbol == "\n":
            ends = [c for c in m["before"] if c in _SENTENCE_END]
            parts.append((ends[-1] if ends else "") + "\n")
            case = None
        else:
            after_text = "".join(parts)[-1:] not in ("", " ", "\n")
            lead = " " if symbol == DASH and after_text else ""
            parts.append(lead + symbol + (" " if m.end() < len(text) else ""))
            if symbol == "...":
                case = "upper"
            else:
                case = "lower" if any(c in _SENTENCE_END for c in m["after"]) else None
        pos = m.end()
    parts.append(_change_case(text[pos:], case))
    return _SPACE_BEFORE_NEWLINE.sub("\n", "".join(parts))  # "trzy kropki nowa linia"


def _change_case(chunk: str, case: str | None) -> str:
    if not chunk or case is None:
        return chunk
    return (chunk[0].lower() if case == "lower" else chunk[0].upper()) + chunk[1:]
