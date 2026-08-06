"""Transcript sanity checks shared by both assessment tiers.

Lives in its own module rather than in fallback.py because azure_client.py
needs the same rule and fallback.py already imports azure_client — putting it
in either one would create a cycle.
"""

from __future__ import annotations

import re
import unicodedata

# Whisper emits one of a small set of subtitle-credit artefacts when fed
# silence or non-speech noise — a well-documented failure mode of the model,
# not a transcript. Treating these as "what the learner said" produces the
# worst possible output: a confident 0/100 with fabricated per-word issues
# ("Said 'sous-titrage' instead of 'un'"). Matched on a normalised form
# (accent- and punctuation-insensitive) so casing/diacritic variants collapse
# together. Substring-matched, since the artefact is sometimes emitted with a
# trailing fragment attached.
_SILENCE_ARTEFACTS = (
    "sous titrage societe radio canada",
    "sous titres realises par la communaute d amara org",
    "sous titres realises para la communaute d amara org",
    "merci d avoir regarde cette video",
    "merci davoir regarde cette video",
    "abonnez vous",
    "a bientot",
    "generique",
)


def normalise_for_match(text: str) -> str:
    """Strip accents/punctuation and collapse whitespace, so the artefact
    table stays readable ASCII and still matches accented recogniser output."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", stripped)).strip()


def transcript_is_unusable(text: str) -> bool:
    """True when a transcript carries no evidence of real speech, so no score
    of any kind may be derived from it.

    Covers the empty/punctuation-only case (Azure returns DisplayText "." for
    silence) and the recogniser-hallucination case.
    """
    normalised = normalise_for_match(text)
    if not normalised:
        return True
    return any(artefact in normalised for artefact in _SILENCE_ARTEFACTS)
