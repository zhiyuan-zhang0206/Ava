from langchain_core.messages import AIMessage, HumanMessage


def ai_message_with_code(code: str, tool_call_id: str = "1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute_code",
                "args": {"code": code},
                "id": tool_call_id,
            }
        ],
    )


def ai_message_multi_tool_use(first_code: str, second_code: str) -> AIMessage:
    import json

    return AIMessage(
        id="ai_multi",
        content=[
            {
                "type": "tool_use",
                "id": "call_00",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": first_code}),
                "index": 0,
            },
            {
                "type": "tool_use",
                "id": "call_01",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": second_code}),
                "index": 1,
            },
        ],
        tool_calls=[
            {"name": "execute_code", "args": {"code": first_code}, "id": "call_00"},
        ],
    )


def human_message(content: str) -> HumanMessage:
    return HumanMessage(content=content)


def stop_turn_ai(content: str = "ok") -> AIMessage:
    return AIMessage(content=content)
