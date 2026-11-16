Classify the latest user message for the {{company}} operations copilot. Return JSON only: {"complexity": "basic" | "complex", "intent": "lookup" | "analysis" | "why" | "action" | "memory" | "smalltalk" | "out_of_scope"}.

- complexity = complex when the answer needs multi-step analysis, a what-if, comparison across several tools or documents, or a synthesis/recommendation. Otherwise basic.
- intent:
  - lookup: a single metric or list (stock cover, OOS risk, promo uplift, a revenue number), or a distributor overview such as "what needs attention at X" (one snapshot call, complexity basic).
  - analysis: open analytical question needing aggregation or comparison.
  - why: asks for causes, rules, contract terms, approval rules or thresholds (who approves N cases) or explanations (documents).
  - action: wants to create, approve or reject a specific replenishment request now (not a question about the approval rule).
  - memory: refers to earlier decisions or preferences ("last time", "what did we decide").
  - smalltalk: greetings, thanks, meta questions about the assistant.
  - out_of_scope: anything unrelated to {{company}} commercial, supply or promotion operations.

Latest user message:
{{message}}
