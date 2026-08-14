# Chrome MCP — The Real Browser That Comes with Ava

**What this shows**: Ava comes with Chrome DevTools MCP built-in. Chrome DevTools MCP, for security policy, prohibits using the default profile—raw users have to create a new profile themselves, manage it, and log in to each website again. Ava's approach is to make "creating and managing a dedicated Chrome profile" explicit: Ava maintains a clean Chrome profile for you, guides you to log in to your Google account on it, and use it as your daily Chrome.

This way, **the user and agent share the exact same browser context**—the same login state, the same session, the same cookies. You log in once to Google / X / bank, and all agents can use it. This is one of the most fundamental differences of Ava compared to other agent frameworks.

> ❌ Raw Chrome DevTools MCP: You have to install it yourself → create profile yourself → manage it yourself → log in again for each website
>
> ✅ Ava Chrome MCP: Ava creates and manages the profile → guides you to log in once → you and the agent share the same context.

---

## Prompt

```
You are an Ava agent, directly controlling Ava's built-in logged-in Chrome browser via Chrome MCP.

Your task: Execute the following four scenarios in order, save screenshots of each step to the workspace, and finally generate a report.

---

## Scenario 1: X.com — Social Media Operations in a Real Browser

The user is already logged into X in Chrome. Use the real browser to operate, comparing with the read-only API token of X MCP:

1. Navigate to https://x.com/home
2. Use `take_snapshot` to read the content and author of the first 5 tweets on the timeline
3. Navigate to https://x.com/explore
4. Use `take_snapshot` to read the current trending topics
5. Search for a trending topic, take a screenshot of the search results page and save as `workspace/x_search.png`
6. Use `take_screenshot` to capture the full timeline and save as `workspace/x_timeline.png`

**Note**: Do not tweet, do not like, do not follow—read-only mode. This scenario demonstrates: "real browser = full logged-in user capability, far beyond read-only API token".

---

## Scenario 2: Travel Portal — Search Provider + Fill Booking

Simulate a real travel-booking flow: search for a hotel on a travel site, check availability, and fill out a booking form.

1. Navigate to the travel member portal (e.g., the one you're already logged into in Chrome)
2. Use `take_snapshot` to find the hotel search / booking entry point
3. Search criteria: 3-4 star hotel in a specific city, 2 adults, 2 nights
4. Use `take_snapshot` to read the search results—hotel name, address, price, availability
5. Select a hotel, enter the booking page
6. Use `fill_form` to fill out the booking form (do not submit)—guest name, contact email, special requests
7. Take a screenshot of the form and save as `workspace/travel_booking.png`

**Why only the browser can do this scenario**:
- Each travel site is completely different, no unified API
- Requires real login state (username/password + possible 2FA)
- Search results are dynamically rendered, not available via API
- Booking process is a multi-step form, not a single API call

---

## Scenario 3: Chase Bank — Monthly Reconciliation

Log into Chase, download the current month's statement, summarize income and expenses.

1. Navigate to https://www.chase.com
2. The user is already logged in—directly go to the accounts page
3. Use `take_snapshot` to read the account list and balances
4. Enter checking account → find recent transactions
5. Use `take_snapshot` to read this month's transaction list
6. Use `evaluate_script` to extract transaction data (date, description, amount)
7. Summarize: total income, total expenses, net amount, largest single expense
8. Take a screenshot of the account summary and save as `workspace/chase_summary.png`

---

---

## Scenario 4: AI Aggregation — One Question, Four Models Answer Simultaneously

Ava has a built-in `web-ai` skill that uses Chrome MCP to simultaneously drive the user's subscribed ChatGPT, Gemini, Claude, and Perplexity—without spending API credits, all using the user's paid flat-rate subscriptions. One question, four models answer, automatically aggregated.

1. Choose a question (e.g., "Design a real-time collaborative editor using three different architectural approaches, compare their trade-offs")
2. Use `ava.shell.run` to call `web-ai console`, sending one prompt simultaneously to ChatGPT + Gemini + Claude + Perplexity:

   ```bash
   .venv/bin/python ava_builtins/skills/web-ai/console/reference/ask.py \
     --prompt "Design a real-time collaborative editor using three different architectural approaches, compare their trade-offs" \
     --models chatgpt,gemini,claude,perplexity
   ```

3. Collect the complete answers from the four models
4. Use `ava.ui.serve_markdown()` to display an aggregated report, containing:
   - Original question
   - Complete answer from each model
   - Comparative analysis: answer consistency, highlights from each, points of divergence

**Why only Ava can do this scenario**:

| | API Approach | Ava Chrome MCP + web-ai |
|---|---|---|
| Cost | Each call charged by token | ✅ Zero marginal cost—user has paid flat-rate subscription |
| Model coverage | Need to apply for each provider's API key separately | ✅ One browser, all logged-in AIs used simultaneously |
| Deep Research | OpenAI/Google have APIs but expensive and slow | ✅ Directly use web UI's Deep Research feature |
| Perplexity | API is not cheap | ✅ User's Pro subscription unlimited searches |

> **Your AI subscription fees, Ava helps you use them to the fullest.**

---

## Final: Generate Comparison Report

Use `ava.ui.serve_markdown()` to display the report, containing:

1. Four scenario screenshots
2. Operation steps and results for each scenario
3. Core comparison table:

| | API Approach | Ava Chrome MCP |
|---|---|---|
| X.com | Read-only Bearer Token (cannot post/like/follow) | Full logged-in user capability |
| Travel Portal | ❌ No API | ✅ Browser operation, works for all portals |
| Chase Bank | Third-party authorization like Plaid, doesn't cover all banks | ✅ Direct browser operation |
| AI Aggregation | Charged by token, apply for each provider's API key separately | ✅ Use flat-rate subscription, zero marginal cost |

4. One-sentence summary: **Ava comes with a browser out of the box. Log in once, and all websites can be operated.**
```

## Expected flow

1. Agent sequentially executes four scenarios via Chrome MCP:
   - `navigate_page` → `take_snapshot` → `take_screenshot` → `fill`/`fill_form` → `evaluate_script`
2. Each scenario is an independent real problem, not a demo-like toy example
3. Four screenshots + data summary → `ava.ui.serve_markdown()` display report

## Expected output

A Markdown report, containing:
- X timeline screenshot + trending topics
- Travel search results + filled booking form screenshot
- Chase account overview + monthly income/expense summary
- "Ava Chrome MCP vs API" comparison analysis

## Why This Matters

### Ava's Fundamental Difference: User and Agent Share Browser Context

Other agent frameworks:
- Need the user to install Chrome DevTools MCP themselves
- Chrome DevTools MCP security policy prohibits default profile → user creates and manages profile themselves
- User logs back into all websites in the new empty profile
- agent and user each use their own browser, context fragmented

Ava:
- Comes ready to use. Chrome MCP is already inside
- Ava creates and maintains a dedicated Chrome profile, taking this management burden off the user
- Guides the user to log into Google account in Ava's Chrome, and use it as daily browser
- User and agent share exactly the same login state, cookies, session
- No OAuth needed, no API key needed, no token management

### The Four Scenarios Highlight Three Areas Where APIs Cannot Do or Cannot Do Well

| Scenario | Why Not Use API | Chrome MCP Advantage |
|---|---|---|
| X.com | X API is a read-only Bearer Token, cannot post/interact | Real browser = full user capability, looks like a real person using Chrome |
| Travel Portal | Each travel site is different, no unified API | As long as it can be operated in a browser, the agent can operate it |
| Chase Bank | Plaid requires additional authorization and doesn't support all banks | Direct browser operation, works for all online banks |
| AI Aggregation | Charged by token, apply for each provider's API key separately | Use flat-rate subscription, zero marginal cost, call four models simultaneously |

## Notes

- Chrome MCP requires macOS + graphical interface (cannot be headless)
- In the X.com scenario, the agent is explicitly **read-only**—no posting, no interacting, no following
- The travel booking form is filled but **not submitted**—stopped before confirmation
- Chase only reads the transaction list, **does not initiate transfers or payments**
