from ..native_contract import contract


def prompt(query, target, answer):
    return contract("locomo_refined").render_accuracy_prompt(
        {"id": query.qa_id, "question": query.text, "gold_answer": target.gold}, answer)


def parse(text):
    return contract("locomo_refined").parse_judge_label(text)
