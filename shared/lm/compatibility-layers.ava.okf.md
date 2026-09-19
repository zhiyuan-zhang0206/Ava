---
type: doc
title: LLM Compatibility Layers
description: '`shared/lm/_anthropic_compat.py` and `_reasoning_compat.py` — provider quirks folded into the chat classes.'
tags:
- shared
- library
- llm-inference
---

# LLM Compatibility Layers

- `_anthropic_compat.py`: `ThinkingTokensChatAnthropic` — ChatAnthropic subclass patching thinking_tokens into usage_metadata (base drops it); shared by claude/deepseek.
- `_reasoning_compat.py`: `ReasoningContentChatModel` — ChatOpenAI subclass folding delta `reasoning_content` into canonical thinking blocks; **used by glm / mimo / qwen**. kimi uses `langchain-moonshot`; reasoning lands in `additional_kwargs["reasoning_content"]`, handled by fan-out + timeline.
