# Disclaimer

This is the text shown in the app sidebar. It is kept in `src/app.py` as
`DISCLAIMER` and in the README; this file is the canonical long form.

## Canonical text

> This assistant provides factual information about 5 HDFC Mutual Fund schemes
> sourced from public Groww pages. It is not investment advice, and it does not
> state, calculate or compare returns, rankings or risk-adjusted performance.
> Figures are a point-in-time snapshot and may be stale — always confirm against
> the official AMC factsheet before acting. Nothing here is a recommendation to
> buy, sell or hold any security.

## Why each clause exists

**"5 HDFC Mutual Fund schemes."** The scope is fixed and small. Answering well
about five schemes is the goal; not answering anything else is the guarantee.

**"Sourced from public Groww pages."** The corpus is five broker-aggregated
pages. They are *not* official AMC, SEBI or AMFI documents, and the app should
not imply otherwise. The five URLs came from the milestone brief; they were not
chosen for regulatory authority.

**"Not investment advice."** Advice questions are refused, not answered softly.
"Can you build me a portfolio?", "Should I buy X?", "Is now a good time?" all
return a refusal.

**"Does not state, calculate or compare returns."** Return and ranking content is
stripped from the corpus during ingest, so the model is not merely asked to avoid
it — it never sees it. This is a data-layer control, not a prompt instruction.

**"Point-in-time snapshot and may be stale."** Every answer carries
`Last updated from sources: YYYY-MM-DD`, the page fetch date. Expense ratios, AUM
and exit loads change. The date is there so a user can judge staleness.

**"Confirm against the official AMC factsheet."** The honest next step for any
figure that matters.

**"Nothing here is a recommendation."** Restates the advice limit in the language
of regulation.

## Not investment advice

- No buy / sell / hold recommendations.
- No fund rankings, comparisons, or "best fund" answers.
- No return, CAGR, or risk-adjusted performance figures.
- No portfolio construction or asset allocation.
- No suitability judgement ("is this right for me?").

## Data handling

Questions are logged to `data/logs/queries.jsonl` for debugging. Before logging,
the question is screened for PII. If any is detected the question is **replaced**
with `[withheld: PII detected]` and only the detected kinds are recorded. The raw
text is never written to disk. Verified: after a PAN test case, the log file
contains zero occurrences of the PAN.

The log holds: timestamp, question (redacted), detected scheme, refusal state and
kind, retrieved chunk IDs, top score, answer, cited URL, contract violations,
latency, and generator/embedding model names.

## Accuracy limits

- Only Direct-Growth plan variants. Other plans are not in the corpus.
- No statement or tax-document download guidance, and no SEBI riskometer. Those
  live on separate Groww help pages that are not allowlisted. The scheme pages
  carry `Groww Rating`, which is a different scale from the SEBI riskometer and
  is never presented as one.
- Lock-in is `3 year` for the ELSS Tax Saver and none for the other four.
- If retrieval finds nothing above the similarity cutoff, the assistant says it
  could not find the answer in its sources. It does not fall back to memory.
