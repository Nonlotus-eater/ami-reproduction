"""Validate complete CLBench judgments and adapt numbered statuses losslessly."""
import json
import re


STATUS_FIELD = "List of Requirement Satisfaction Status"
JUDGE_PROTOCOL = "clbench-indexed-status-v1"


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in CLBench judgment")
        result[key] = value
    return result


def parse_judgment(text, count, *, indexed=False):
    """Reject partial/repeated judgments; never infer or repair the model's score.

    N/A is retained for compatibility with recorded native diagnostic statuses.
    It cannot justify a passing score, or replace the explicit no in a failure.
    """
    if type(count) is not int or count < 1:
        raise ValueError("CLBench requires a positive rubric count")
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    payload = json.loads(clean, object_pairs_hook=unique_keys)
    if not isinstance(payload, dict):
        raise ValueError("CLBench judgment must be a JSON object")
    statuses = payload.get(STATUS_FIELD)
    if indexed:
        expected = {str(i) for i in range(1, count + 1)}
        if not isinstance(statuses, dict) or set(statuses) != expected:
            raise ValueError("Judge must return exactly one numbered status per rubric")
        statuses = [statuses[str(i)] for i in range(1, count + 1)]
    if not isinstance(statuses, list) or len(statuses) != count:
        raise ValueError("Judge status count differs from rubric count")
    if any(not isinstance(v, str) or v not in ("yes", "no", "N/A") for v in statuses):
        raise ValueError("Invalid CLBench rubric status")
    score = payload.get("Overall Score")
    if not ((type(score) is int and score in (0, 1)) or
            (isinstance(score, str) and score in ("0", "1"))):
        raise ValueError("Invalid strict rubric judge score")
    if ((int(score) == 1 and not all(v == "yes" for v in statuses)) or
            (int(score) == 0 and "no" not in statuses)):
        raise ValueError("Overall Score inconsistent with strict all-rubrics rule")
    if not isinstance(payload.get("Grading Rationale"), str):
        raise ValueError("CLBench judgment requires a string rationale")
    return {**payload, STATUS_FIELD: statuses}


def indexed_status_addendum(count):
    """Reuse the historical format adapter without changing grading criteria."""
    return (
        '\n\nOUTPUT FORMAT ADAPTER (grading criteria are unchanged):\n'
        f'There are exactly {count} rubric requirements. Evaluate every one independently. '
        'Keep the native JSON fields, but encode "List of Requirement Satisfaction Status" as a JSON OBJECT '
        f'with exactly the string keys "1" through "{count}", in numerical order, '
        'instead of an unnumbered array. Each value must be exactly "yes" or "no" for that numbered requirement. '
        'Stop the object after the final numbered requirement; do not repeat entries. '
        '"Grading Rationale" must be concise (at most 180 words); do not copy the rubric or student answer. '
        'Apply the original strict Overall Score rule: 1 only if every requirement is satisfied, otherwise 0. '
        'Return only the complete JSON object, including "Overall Score".'
    )
