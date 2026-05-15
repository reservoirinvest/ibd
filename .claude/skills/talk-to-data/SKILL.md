\---

name: talk-to-data

description: Build LLM integration for Streamlit dashboard to query pickled data

disable-model-invocation: true

\---



\## Task: Implement talk-to-data feature



1\. Load pickled data from ./data/ on dashboard startup

2\. Create LLM query handler that:

&#x20;  - Accepts user questions

&#x20;  - Loads minimal relevant data subset

&#x20;  - Sends to Claude API with data context

&#x20;  - Returns response to Streamlit UI

3\. Add model selector dropdown (Claude Haiku/Sonnet/Opus)

4\. Cache responses to reduce token usage



\## Token optimization

\- Use Haiku for simple queries (cheapest)

\- Use Sonnet for complex analysis (balanced)

\- Use Opus only for reasoning-heavy tasks

