from ..native_contract import contract


def render(query, context, speakers):
    return contract("locomo_refined").render_answer_prompt({"question": query.text,
        "speaker_1_name": speakers[0], "speaker_2_name": speakers[1],
        "speaker_1_memories": context, "speaker_2_memories": ""})
