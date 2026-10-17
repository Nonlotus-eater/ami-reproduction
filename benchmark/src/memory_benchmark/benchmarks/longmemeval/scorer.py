from ..native_contract import contract


def prompt(query, target, answer):
    return contract("longmemeval_s").render_accuracy_prompt(
        {"id": query.qa_id, "question": query.text, "gold_answer": target.gold}, answer)


def parse(text):
    return contract("longmemeval_s").parse_judge_label(text)
