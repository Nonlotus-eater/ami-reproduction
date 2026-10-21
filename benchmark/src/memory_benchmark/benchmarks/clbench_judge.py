"""Validate complete CLBench judgments and adapt numbered statuses losslessly."""
import json
import re


STATUS_FIELD = "List of Requirement Satisfaction Status"
JUDGE_PROTOCOL = "clbench-indexed-status-v1"
STRICT_JUDGE_PROTOCOL = "clbench-indexed-status-v2"


class JudgeProtocolError(ValueError):
    """A completed Judge response violates the frozen grading protocol."""


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JudgeProtocolError("Duplicate JSON key in CLBench judgment")
        result[key] = value
    return result


def parse_judgment(text, count, *, indexed=False, strict_statuses=False):
    """Reject partial/repeated judgments; never infer or repair the model's score.

    N/A is retained for compatibility with recorded native diagnostic statuses.
    It cannot justify a passing score, or replace the explicit no in a failure.
    """
    if type(count) is not int or count < 1:
        raise JudgeProtocolError("CLBench requires a positive rubric count")
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        payload = json.loads(clean, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as error:
        raise JudgeProtocolError("Malformed or incomplete CLBench JSON") from error
    if not isinstance(payload, dict):
        raise JudgeProtocolError("CLBench judgment must be a JSON object")
    statuses = payload.get(STATUS_FIELD)
    if indexed:
        expected = {str(i) for i in range(1, count + 1)}
        if not isinstance(statuses, dict) or set(statuses) != expected:
            raise JudgeProtocolError("Judge must return exactly one numbered status per rubric")
        statuses = [statuses[str(i)] for i in range(1, count + 1)]
    if not isinstance(statuses, list) or len(statuses) != count:
        raise JudgeProtocolError("Judge status count differs from rubric count")
    allowed = ("yes", "no") if strict_statuses else ("yes", "no", "N/A")
    if any(not isinstance(v, str) or v not in allowed for v in statuses):
        raise JudgeProtocolError("Invalid CLBench rubric status")
    score = payload.get("Overall Score")
    if not ((type(score) is int and score in (0, 1)) or
            (isinstance(score, str) and score in ("0", "1"))):
        raise JudgeProtocolError("Invalid strict rubric judge score")
    if ((int(score) == 1 and not all(v == "yes" for v in statuses)) or
            (int(score) == 0 and "no" not in statuses)):
        raise JudgeProtocolError("Overall Score inconsistent with strict all-rubrics rule")
    if not isinstance(payload.get("Grading Rationale"), str):
        raise JudgeProtocolError("CLBench judgment requires a string rationale")
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


def strict_status_addendum(count):
    return indexed_status_addendum(count) + (
        '\nPROTOCOL VALIDATION: "N/A", "not applicable", null and omitted requirements are forbidden. '
        'For each requirement, decide whether the answer satisfies it under the original grading criteria '
        'and output exactly "yes" or "no". Do not invent additional grading criteria. '
        'Before returning, verify Overall Score equals 1 if and only if every numbered value is "yes"; '
        'otherwise it must equal 0. An inconsistent response is invalid and must be judged again.\n'
    )


def judgment_response_format(count):
    return {"type": "json_schema", "json_schema": {"name": "clbench_judgment", "strict": True,
        "schema": {"type": "object", "additionalProperties": False,
            "properties": {"Grading Rationale": {"type": "string"},
                STATUS_FIELD: {"type": "object", "additionalProperties": False,
                    "properties": {str(i): {"type": "string", "enum": ["yes", "no"]} for i in range(1, count + 1)},
                    "required": [str(i) for i in range(1, count + 1)]},
                "Overall Score": {"type": "integer", "enum": [0, 1]}},
            "required": ["Grading Rationale", STATUS_FIELD, "Overall Score"]}}}
