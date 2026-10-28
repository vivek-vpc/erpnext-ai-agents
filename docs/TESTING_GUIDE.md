# Help me test it (about 30 minutes)

This is an early concept. I would rather hear what is wrong now than after I build more on it. You do not need ERPNext for steps 1 to 4.

## 1. Start the demo (5 minutes)

Follow section 1 of the [README](../README.md#1-try-it-no-erpnext-no-accounts): Docker is the quickest.

## 2. Follow one reorder from start to finish (10 minutes)

1. Open the console and look at **Needs your OK**. Read one stock alert and its "Why the assistant is asking".
2. Press **Start reorder** on a reorder request. Within three minutes a purchase order request appears under **Orders and requisitions**.
3. Read **Why this vendor** and **The other vendors**. Would you have chosen the same one? Is anything you would want to know missing?
4. Try **Change quantity**, **Change vendor** and **Don't order this**. Press **Create the order** only if you are curious: in the demo it creates nothing outside the demo data.
5. Open **History** and find the decision you just made. Is the reason still clear a week later?

## 3. Ask it something (5 minutes)

Open **Ask** and try questions such as "What is running low?", "How long will Hydraulic Oil last?" or "Which vendor is late?". Note any answer that is wrong, vague or too long.

## 4. Try to break it (5 minutes, optional)

Use a wrong access key, an odd quantity, or a request you think it should refuse. Nothing is ever ordered without a person pressing the button, so tell me if you find a way around that.

## 5. On your own ERPNext (optional, read-only first)

If you run ERPNext v16, follow section 4 of the README, then `erp-agents erp-check` and `erp-agents run-once --dry-run`. Compare its numbers with your Stock Balance report. Use a test site or a copy of your data. Other ERPNext versions have not been tried, so a "does not work on v15" report is useful.

## 5b. Check the AI's answers (optional, 5 minutes)

You can see what the assistant answers to the questions the project checks on every change, without a language model or ERPNext:

```bash
pip install -e ".[dev,agents]"
python -m tools.evals --show item-stock-by-code      # one question and the answer it really gives
python -m tools.evals                                 # all of them, and whether each is right
```

If you find a question that gets a wrong or confusing answer, the best bug report is a case: a few lines of YAML in `tests/evals/cases/` ([docs/EVALUATION.md](EVALUATION.md) shows how). A plain description in an issue is fine too.

## What I would like to hear

Please answer these four in a [feedback issue](https://github.com/vivek-vpc/erpnext-ai-agents/issues/new?template=tester_feedback.yml) or in [Discussions](https://github.com/vivek-vpc/erpnext-ai-agents/discussions):

1. What did the agent get wrong, or explain badly?
2. What should it always ask you before acting?
3. Which part of the setup was hardest?
4. Would you run it on live data, and what would have to change first?

Please leave out real company names, prices and personal data. Security problems go to a [private advisory](https://github.com/vivek-vpc/erpnext-ai-agents/security/advisories/new), not a public issue.
