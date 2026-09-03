# Multi-Model Config Overlay Demo

**What this shows**: `config_overlay` model selection capability — distributing the same task to Claude, GPT, and DeepSeek, and comparing their answer quality, style, and reasoning depth.

## Prompt

```
Your task: Use config_overlay to demonstrate Ava's multi-model switching capability.

**Steps**:

1. Choose an interesting question that does not rely on time-sensitive information (for example: philosophical thinking, code design trade-offs, creative writing) as a common test question for the three models.

2. Use `ava.agents.spawn()` to create 3 agents, each specifying a different `llm_model`:
   - `claude-sonnet-4-6`
   - `gpt-5.4-mini`
   - `deepseek-v4-pro`

   Example:
   ```python
   ava.agents.spawn(
       prompt=question_prompt,
       config_overlay={"llm_model": "claude-sonnet-4-6"},
       label="demo-claude"
   )
   ```

3. Wait for the three agents to finish (read their `last_message`), then compare the three answers:
   - Structure and quality of the answers
   - Depth of reasoning
   - Style differences
   - Presence of hallucination or errors

4. Render the comparison report to HTML and display it with `ava.ui.serve()`, including:
   - The original question
   - The full answers from the three models
   - Your comparative analysis

**Requirements**:
- Each agent receives exactly the same prompt
- The prompt should be deep enough to reveal model differences (don't ask "What is 1+1?")
- The comparison report should be in Chinese
```

## Expected flow

1. Agent designs a test question (e.g., "Practical trade-offs between purely functional programming and object-oriented programming in large projects")
2. Spawn three agents in parallel, each using a different model
3. Collect the three answers
4. Output the comparison analysis report to the UI

## Expected output

A Markdown report containing:
- Test question
- Claude's answer + analysis
- GPT's answer + analysis
- DeepSeek's answer + analysis
- Comprehensive comparison (table + text)

## Why this matters

`config_overlay` allows each agent to choose a different model without modifying the global configuration. This is especially useful in the following scenarios:

- **Comparative evaluation**: See how different models perform on the same task and choose the most suitable one
- **Cost optimization**: Use cheaper models for simple tasks (e.g., deepseek-v4-flash) and powerful models for complex reasoning (e.g., claude-opus-4-8)
- **Complementary strengths**: Some models excel at coding, others at creative writing, assign based on needs
- **Gradual migration**: When a new model goes live, try it on some agents first, then fully switch after validation
