---
type: doc
title: LLM Compatibility Layers
description: '`base/lm/compat/anthropic_thinking.py` and `openai_reasoning.py` — provider quirks folded into the chat classes.'
tags:
- base
- library
- llm-inference
---

# LLM Compatibility Layers

- `anthropic_thinking.py`: `ThinkingTokensChatAnthropic` — ChatAnthropic subclass patching thinking_tokens into usage_metadata (base drops it); shared by claude/deepseek.
- `openai_reasoning.py`: `ReasoningContentChatModel` — ChatOpenAI subclass folding delta `reasoning_content` into canonical thinking blocks; **used by glm / mimo / qwen**. kimi uses `langchain-moonshot`; reasoning lands in `additional_kwargs["reasoning_content"]`, handled by fan-out + timeline.
