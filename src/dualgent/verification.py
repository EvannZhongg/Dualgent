"""Verification consumes fresh facts, never model assertions or action acknowledgements."""

from .contracts import Check, Observation


def evaluate(check: Check, observation: Observation) -> dict:
    if check.surface_id != observation.surface.id:
        raise ValueError("check and observation refer to different surfaces")
    if check.kind == "element":
        matches = [
            e for e in observation.elements if e.role == check.role and e.name == check.expected
        ]
        found = len(matches) == 1 and (check.value is None or matches[0].value == check.value)
        # An absent check cannot prove absence in a truncated observation.
        passed = found if check.present else not matches and not observation.truncated
        evidence = [e.model_dump() for e in matches]
    else:
        actual = {
            "text": observation.text,
            "title": observation.surface.title,
            "url": observation.surface.url or "",
        }[check.kind]
        found = check.expected in actual
        passed = found if check.present else not found and not observation.truncated
        evidence = actual[:16000]
    return {
        "check": check.model_dump(),
        "passed": passed,
        "observation_id": observation.id,
        "evidence": evidence,
    }
