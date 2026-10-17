"""Native prompt/parse functions with the shared reader and guarded transport."""
import json
import math
import re
from .native_contract import contract
from .clbench_judge import indexed_status_addendum, parse_judgment, STATUS_FIELD, JUDGE_PROTOCOL


def render(query, context, model):
    native = contract(query.benchmark)
    if query.benchmark == "beam":
        return native.render_answer_prompt({"question": query.text, "context": context})
    if query.benchmark == "clbench":
        return native.render_answer_prompt_clbench(system_prompt=query.system_prompt, memories=context, question=query.text)
    history = [{"role": "user", "content": context or "(no memories)"}]
    if query.benchmark == "personamem_v1":
        return native.official_messages({"question": query.text, "all_options": query.all_options,
                                        "context_messages": history}, model)
    return native.official_generative_messages({"user_query": query.text, "chat_history": history})


def evaluate(query, target, prediction, client, runtime):
    native = contract(query.benchmark)
    def judge(prompt, max_tokens=None):
        return client.complete(prompt, stage="judge", max_tokens=max_tokens or runtime["judge_max_tokens"],
                               temperature=runtime["judge_temperature"])
    result = {"id": query.qa_id}
    if query.benchmark == "personamem_v1":
        correct, extracted = native.official_extract_answer(prediction, target.gold)
        return {**result, "is_correct": correct, "predicted_answer": extracted,
                "predicted_options": sorted(native.extract_option_set(extracted)),
                "gold_option": native.extract_gold_option(target.gold)}
    if query.benchmark == "personamem_v2":
        preference = target.scoring_data["preference"]
        template = native.JUDGE_PROMPT_NARROW_NEGATIVE if preference.lower().startswith("do not") else native.JUDGE_PROMPT_NARROW_POSITIVE
        reply = judge(template.format(user_query=query.text, preference=preference, model_response=prediction))
        # The native extractor returns zero on parse failure. A broken judge is unscored locally.
        if not re.search(r"\\boxed\s*\{|score[:\s]+[0-9]|rating[:\s]+[0-9]|[0-9]*\.[0-9]+\s*/\s*1", reply.text, re.I):
            raise ValueError("Unparseable personalization judge")
        return {**result, "score": native.extract_boxed_score(reply.text), "judge_text": reply.text,
                "judge_model": reply.model}
    if query.benchmark == "clbench":
        rubrics = native._normalize_rubrics(target.scoring_data["rubrics"])
        if not rubrics:
            raise ValueError("No native rubrics")
        prompt = native.rubric_judge_prompt(rubrics_text=native._build_rubrics_text(rubrics),
                                          model_output=native._normalize_model_output(prediction))
        reply = judge(prompt + indexed_status_addendum(len(rubrics)))
        payload = parse_judgment(reply.text, len(rubrics), indexed=True)
        statuses = payload[STATUS_FIELD]
        return {**result, "rubric_clbench_score": float(native._coerce_score(payload)),
                "rubric_clbench_rationale": str(payload.get("Grading Rationale", "")),
                "rubric_clbench_requirement_status": statuses,
                "rubric_clbench_requirement_ratio": native._compute_requirement_ratio(statuses),
                # Retain the native list representation for downstream readers,
                # alongside the model's unchanged numbered response and protocol.
                "judge_response": json.dumps(payload, ensure_ascii=False),
                "judge_response_raw": reply.text, "judge_protocol": JUDGE_PROTOCOL,
                "judge_model": reply.model}
    rubrics = native.rubric_items(target.scoring_data)
    reply = judge(native.render_batch_judge_prompt(query.text, prediction, rubrics))
    scores = native.parse_rubric_scores(reply.text, len(rubrics))
    result.update(question_type=target.question_type, judge_model=reply.model, judge_response=reply.text,
                  rubric_scores=[{"rubric": r, **s} for r, s in zip(rubrics, scores)],
                  llm_judge_score=sum(s["score"] for s in scores) / len(scores))
    if target.question_type == "event_ordering":
        used, aligned = set(), []
        for candidate in prediction.split("\n"):
            match = None
            for i, reference in enumerate(rubrics):
                if i in used:
                    continue
                equivalence = judge([{"role": "system", "content": native.EQUIVALENCE_SYSTEM_PROMPT},
                    {"role": "user", "content": f"First snippet: {reference}\n\nSecond snippet: {candidate}"}], 8)
                if "yes" in equivalence.text.casefold():
                    match = i
                    break
            aligned.append(candidate if match is None else rubrics[match])
            if match is not None:
                used.add(match)
        result["event_ordering"] = native.event_ordering_metrics(rubrics, aligned)
    return result
