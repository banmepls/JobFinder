"""Language detection for vacancy text + which languages the candidate accepts.

Why this exists: asking the model to notice that a description is written in
Italian does not work reliably - it reads Italian perfectly well and happily
scores the job 85 against an "English only" instruction. So the gate is made
deterministic here: detect the language of the description in code, compare it
with the languages the candidate named in their instructions, and cap the score
before the model's number is ever trusted.

Detection is stopword-frequency based (no extra dependency). Precision matters
far more than recall in one direction: wrongly calling an ENGLISH job foreign
would hide a good job, so a foreign verdict needs a clear margin over English.
"""

from __future__ import annotations

import re

# High-frequency function words per language. Deliberately short lists of words
# that are common inside their own language and rare in the others.
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "for", "with", "you", "our", "will", "are", "your",
           "that", "this", "have", "from", "work", "team", "who", "about", "we"},
    "de": {"und", "der", "die", "das", "mit", "für", "von", "ist", "ein", "eine",
           "wir", "dich", "deine", "unser", "bei", "auf", "nicht", "als",
           "werden", "sowie", "oder", "zum", "zur"},
    "fr": {"les", "des", "une", "pour", "avec", "vous", "nous", "est", "dans",
           "sur", "aux", "par", "qui", "plus", "votre", "notre", "ainsi",
           "cette", "leur", "chez"},
    "it": {"il", "lo", "gli", "del", "della", "che", "per", "con", "una", "non",
           "sono", "nel", "alla", "delle", "come", "anche", "più", "nostro",
           "azienda", "ricerca", "attività", "sede"},
    "es": {"los", "las", "del", "que", "para", "con", "una", "por", "más",
           "como", "son", "nuestro", "empresa", "puesto", "trabajo", "sobre",
           "desde", "este"},
    "pt": {"os", "as", "dos", "das", "que", "para", "com", "uma", "por", "mais",
           "como", "são", "nossa", "você", "empresa", "trabalho", "sobre"},
    "nl": {"het", "een", "van", "voor", "met", "zijn", "niet", "aan", "bij",
           "ook", "worden", "onze", "jij", "wij", "jouw", "naar", "dat"},
    "pl": {"praca", "oraz", "dla", "nie", "się", "jest", "przez", "lub", "są",
           "które", "naszej", "będziesz", "wymagania", "obowiązki"},
    "sv": {"och", "att", "av", "för", "som", "är", "ett", "till", "vi", "med",
           "den", "det", "inte", "kan", "hos", "dig"},
    "da": {"og", "til", "af", "for", "med", "der", "som", "har", "vil", "du",
           "vi", "ikke", "hos", "vores"},
    "no": {"og", "til", "av", "for", "med", "som", "har", "vil", "du", "vi",
           "ikke", "hos", "våre", "eller"},
    "fi": {"ja", "on", "sekä", "tai", "että", "myös", "olet", "meillä",
           "työtä", "kanssa", "hakemus"},
    "cs": {"na", "se", "je", "pro", "že", "do", "nebo", "jako", "práce",
           "naše", "budete"},
    "ro": {"și", "de", "la", "cu", "pentru", "în", "este", "care", "sau",
           "din", "nostru", "vei"},
    "hu": {"és", "az", "egy", "hogy", "nem", "vagy", "van", "meg", "csapat",
           "munkatárs"},
    "tr": {"ve", "bir", "ile", "için", "olarak", "bu", "çalışma", "deneyim",
           "ekibimize"},
}

# Cyrillic is decided by script, then by a few distinctive words.
_UK_MARKERS = {"та", "що", "для", "роботи", "досвід", "компанії", "ми"}
_RU_MARKERS = {"и", "в", "на", "для", "работы", "опыт", "компании", "мы"}

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English", "de": "German", "fr": "French", "it": "Italian",
    "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "tr": "Turkish",
    "uk": "Ukrainian", "ru": "Russian",
}

# How the candidate might name a language in their instructions.
_NAME_TO_CODE: dict[str, str] = {
    "english": "en", "german": "de", "deutsch": "de", "french": "fr",
    "italian": "it", "spanish": "es", "castilian": "es", "portuguese": "pt",
    "dutch": "nl", "flemish": "nl", "polish": "pl", "swedish": "sv",
    "danish": "da", "norwegian": "no", "finnish": "fi", "czech": "cs",
    "romanian": "ro", "hungarian": "hu", "turkish": "tr", "ukrainian": "uk",
    "russian": "ru",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# Below this many recognisable words the sample is too short to judge.
_MIN_WORDS = 40
# A foreign verdict needs this many hits AND this much of an edge over English.
_MIN_HITS = 5
_MARGIN = 1.5


def detect(text: str | None) -> str | None:
    """Best-guess ISO code for `text`, or None when it can't be judged.

    Returns None rather than guessing on short or ambiguous input - an unknown
    language must never be treated as a violation."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < _MIN_WORDS:
        return None

    if _CYRILLIC.search(text or ""):
        uk = sum(1 for w in words if w in _UK_MARKERS)
        ru = sum(1 for w in words if w in _RU_MARKERS)
        return "uk" if uk > ru else "ru"

    sample = set(words)
    counts = {code: len(sample & sw) for code, sw in _STOPWORDS.items()}
    en = counts.get("en", 0)
    best, hits = max(counts.items(), key=lambda kv: kv[1])
    if best == "en" or hits < _MIN_HITS or hits < en * _MARGIN:
        # Not clearly foreign. Only call it English when English words actually
        # showed up; otherwise stay undecided.
        return "en" if en >= _MIN_HITS else None
    return best


def name(code: str | None) -> str:
    return LANGUAGE_NAMES.get(code or "", code or "unknown")


def accepted_languages(instructions: str | None) -> set[str]:
    """Languages the candidate named in their own instructions.

    Empty set = they said nothing about language, so no gate is applied."""
    text = (instructions or "").lower()
    return {code for word, code in _NAME_TO_CODE.items() if re.search(rf"\b{word}\b", text)}


def violates(description: str | None, instructions: str | None) -> str | None:
    """Name of the description's language when the candidate did not list it -
    i.e. a hard language blocker. None when there is no violation (or no call)."""
    return _written_in_other(description, accepted_languages(instructions))


def _written_in_other(description: str | None, accepted: set[str]) -> str | None:
    if not accepted:
        return None
    code = detect(description)
    if code is None or code in accepted:
        return None
    return name(code)


def blocker(description: str | None, accepted: set[str]) -> str | None:
    """Name of the language that shuts the candidate out of this advert - it is
    written in it, or demands it - judged against a set of language codes rather
    than the instructions' wording. None when `accepted` is empty."""
    return _written_in_other(description, accepted) or _required_missing(description, accepted)


# --------------------------------------------------------------------------- #
# The company search: which languages the CV lists                             #
# --------------------------------------------------------------------------- #

# The languages the Companies page offers: the working languages of the
# countries it searches - Switzerland, Austria and Benelux - and English.
OFFERED = ("en", "fr", "de", "it", "nl")


def detect_short(text: str | None, among: tuple[str, ...] = ("de", "fr", "it")) -> str | None:
    """The language of a few formal lines - a registered purpose - where
    `detect` refuses to judge (under 40 words). Two function words and a strict
    lead over the runner-up, else None. Measured on 200 register purposes on
    11 Nov 2026: 197 decided, every one the language of the company's canton."""
    words = {w.lower() for w in _WORD.findall(text or "")}
    counts = sorted(((len(words & _STOPWORDS[c]), c) for c in among), reverse=True)
    (best_n, best), (second_n, _) = counts[0], counts[1]
    return best if best_n >= 2 and best_n > second_n else None


# Language names as a CV may write them, beyond the English ones above.
_CV_NAMES: dict[str, str] = {
    **_NAME_TO_CODE,
    "englisch": "en", "anglais": "en", "inglese": "en",
    "français": "fr", "francais": "fr", "französisch": "fr", "francese": "fr",
    "allemand": "de", "tedesco": "de",
    "italiano": "it", "italien": "it", "italienisch": "it",
    "español": "es", "espagnol": "es", "spanisch": "es",
}
_CV_LANG_HEADING = re.compile(
    r"^[\W_]*(languages?(?: skills)?|sprachen|sprachkenntnisse|langues|lingue|idiomas)\b",
    re.I | re.M)
# The end of the languages section: a blank line, then the next heading.
_CV_NEXT_HEADING = re.compile(r"\n\s*\n(?=[^\W\d_][^\W\d_ &/-]*(?: [^\W\d_&/-]+)*\s*\n)")
# A level too low to work in.
_BEGINNER = re.compile(r"\b(a1|a2|basic|beginner|elementary|notions?|grundkenntnisse)\b", re.I)


def cv_languages(cv_text: str | None) -> set[str]:
    """Languages listed in the CV's own languages section - not every language
    the CV happens to name ("the German market"), and not one at a beginner's
    level ("German (A2)"). Empty when the CV has no such section."""
    text = cv_text or ""
    found: set[str] = set()
    for heading in _CV_LANG_HEADING.finditer(text):
        section = _CV_NEXT_HEADING.split(text[heading.end(1):heading.end(1) + 600], 1)[0]
        names = [(m.start(), m.end(), code) for m in re.finditer(r"[^\W\d_]+", section)
                 if (code := _CV_NAMES.get(m.group(0).lower()))]
        for i, (_, end, code) in enumerate(names):
            # A language's level is what follows its name, up to the next
            # language or separator: "English (C1), German (A2)" is two levels.
            stop = names[i + 1][0] if i + 1 < len(names) else len(section)
            level = re.split(r"[|;\n]", section[end:stop], maxsplit=1)[0]
            if not _BEGINNER.search(level):
                found.add(code)
    return found


# An explicit demand to WORK in a named language, as opposed to the advert being
# written in it. Precision over recall: fire only on wording that clearly
# requires the language, never when it is offered as a "plus".
#
# The cue must sit RIGHT NEXT TO the language name, not merely in the same
# sentence - a loose window turned "Requirements: … the German market" and
# "manage Spanish clients (Required)" into false blockers, because a generic
# "Requirements" heading is only ever a few words from any noun. So it is two
# tight patterns: a proficiency cue immediately before the language, or a
# requirement phrase immediately after it.
_PROF_BEFORE = (
    r"(?:fluent|fluency\s+in|native|proficient\s+in|proficiency\s+in|"
    r"command\s+of|good\s+knowledge\s+of|knowledge\s+of|speaks?|spoken|"
    r"written\s+and\s+spoken|mother[\s-]?tongue(?:\s+in)?)"
)
_REQ_AFTER = (
    r"(?:language\s+)?(?:skills?\s+)?(?:is\s+|are\s+)?"
    r"(?:required|mandatory|essential|compulsory|a\s+must|"
    r"the\s+working\s+language|at\s+(?:a\s+)?(?:native|c1|c2|b2)\s+level)"
)
_PLUS_CUES = (
    "a plus", "plus,", "plus.", "plus)", "plus ", "nice to have", "nice-to-have",
    "advantage", "advantageous", "beneficial", "bonus", "desirable", "an asset",
    "asset,", "asset.", "preferred", "preferable", "would be a", "welcome",
    "appreciated", "not required", "no need", "optional",
)


def required_language_missing(description: str | None,
                              instructions: str | None) -> str | None:
    """Name of a language the advert explicitly REQUIRES that the candidate did
    not list - the hard blocker hiding in an otherwise-English advert.

    `violates` catches an advert WRITTEN in a foreign language; this catches an
    English advert that DEMANDS one ("fluent Hungarian required"). None when the
    language is only offered as a "plus", the candidate listed it, or they named
    no languages of their own to judge against.

    The cue must be adjacent to the language name (see `_PROF_BEFORE`/`_REQ_AFTER`),
    so "the German market" and "Spanish clients" - a language name near a generic
    requirement word - are not mistaken for a language requirement."""
    return _required_missing(description, accepted_languages(instructions))


def _required_missing(description: str | None, accepted: set[str]) -> str | None:
    if not accepted:
        return None
    text = description or ""
    for word, code in _NAME_TO_CODE.items():
        if code in accepted or code == "en":
            continue
        lang = re.escape(word)
        # The cue is immediately followed by the language (bar an optional
        # "in"): "knowledge of German" is a language demand, "knowledge of the
        # German market" is not - the article + noun is what tells them apart.
        pattern = re.compile(
            rf"{_PROF_BEFORE}\s+(?:in\s+)?\b{lang}\b"
            rf"|\b{lang}\b\s+{_REQ_AFTER}",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            window = text[max(0, m.start() - 30): m.end() + 30].lower()
            if any(p in window for p in _PLUS_CUES):
                continue
            return name(code)
    return None
