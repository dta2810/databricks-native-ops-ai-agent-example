# Example usage

Example questions and what to expect. Open the app as a planner. Keep a second person (an approver) ready for the approval step, and open the MLflow experiment in another tab. Run `scripts/refresh_dates.py` and `./deploy.sh <t> smoke` first ([OPERATIONS.md](OPERATIONS.md)).

## 1. Out-of-stock risk and a replenishment proposal

Ask: *"Which Volt Original positions in Texas are at risk of running out of stock? Propose a top-up for the worst one."*

- Expected: a short answer with the positions by days of cover, then an approval card for one replenishment request. The quantity follows the replenishment policy (never above 1.5× its recommendation).
- The request appears in **Requests** as `proposed`. The approver opens it and approves or rejects with a reason. The requester cannot decide their own request (four-eyes).
- Behind it: governed UC functions (stock cover, suggestion), a proposal row in Lakebase, the decision written to the decision log.

## 2. Decision memory

Ask: *"Harbor Lager 355ml Bottle is low at FL-D2. Should we top it up?"*

- Expected: the agent recalls the earlier decision about that distributor (on credit hold) and does not propose a top-up without flagging it. The decision log is shared by the team, so one person's decision informs everyone's next answer.

## 3. Why a promotion underperforms

Ask: *"Why is promo PR-0422 underperforming at CA-D1, and is any other California distributor at risk because of it?"*

- Expected: numbers from the governed promotion functions, and the cause from the document corpus (the promotion brief and the field report), with sources cited.
- Behind it: hybrid search over Lakebase (vector + BM25) and a Genie space for the commercial numbers.

## 4. Ask the data directly

- *"Which market has the highest net revenue in the last 13 weeks?"* → Commercial & Promotions Genie space.
- *"Volt sell-in versus sell-out in Texas over the last 8 weeks"* → Supply & Replenishment Genie space.

## 5. What the guards do

- More than three proposals in one turn, or more than ten open requests per person, are refused by the server with a clear message, whatever the prompt says.
- A repeated proposal for the same distributor and SKU returns the open request instead of a duplicate; amend it instead.
- Sending messages faster than the per-person limit returns "Too many messages"; a second message while a turn is running in the same thread is refused until it finishes.

## 6. See the quality loop

In the MLflow experiment (viewer group):

- **Traces**: one `bev_ops_turn` trace per message, grouped by session, with every tool call, Genie query and LLM call. User identities are masked.
- **Evaluation runs**: scores from `./deploy.sh <t> eval --apply`.
- **Monitors / scorers**: the online monitors scoring live traffic.
- **Prompts**: `bev_ops_system` and `bev_ops_router` versions in the Prompt Registry; the `production` alias is what the app loads.

To change behaviour, edit a prompt template, run `mlflow_setup/02_prompts.py`, evaluate the new version, and move the alias when the scores hold.
