"""LangChain chat-model subclasses that recover provider data the base client drops.

`anthropic_thinking.py` (`ThinkingTokensChatAnthropic`) and `openai_reasoning.py`
(`ReasoningContentChatModel`) each patch one chunk-conversion seam so a
provider quirk the upstream LangChain client discards — Anthropic's
`thinking_tokens` usage detail, an OpenAI-compatible delta's
`reasoning_content` field — survives into the message the rest of the
framework sees. Import the submodule you need directly
(`from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic`);
this init keeps no facade.
"""
