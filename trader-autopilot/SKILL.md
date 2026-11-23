---
name: trader-autopilot
description: >-
  Autopilot. Runs the whole day-trader pipeline on its own on an Alpaca PAPER
  account: scanner -> news -> entry -> exit -> timemachine -> budget -> risk ->
  broker, then watches the bracket's stop and target and flattens everything by
  15:55 New York time. Starts, stops, pauses and reports the autopilot and its
  dashboard. Use when the user says "start the autopilot", "stop the bot",
  "pause trading", "autopilot status", "how did the bot do today", "open the
  dashboard", "tune the autopilot", "Autopilot starten", "Bot pausieren".
argument-hint: "[start | stop | pause | resume | status | tune]"
---

# trader-autopilot

The autopilot is the eleven-skill pack run on a clock instead of by hand. Every step
is the pack's own tool, reading and writing the same `trader/` files. It replaces
exactly one gate: the user's typed yes in trader-broker. The user switched that gate
off for the **paper account only** by turning the autopilot on. Everything else
stays: trader-risk must PASS each exact order, the Time Machine must show an edge,
and the account must be an Alpaca paper account.

`${CLAUDE_SKILL_DIR}/..` is the project root (the folder that holds `autopilot/`).

```bash
./start.sh                                   # autopilot + dashboard (http://127.0.0.1:8765)
./stop.sh
python3 autopilot/autopilot.py status        # one line
python3 autopilot/autopilot.py pause         # no new entries; open trades still managed
python3 autopilot/autopilot.py resume
python3 tuning/fetch.py && python3 tuning/backtest.py && python3 tuning/gate_eval.py   # re-tune
python3 -m unittest discover -s tests        # 300+ offline tests, incl. a full autopilot day
```

## The day

| New York time | what it does |
| --- | --- |
| 09:00-09:29 | trader-scanner `daily` over the whole universe; the most traded hits join the fixed list in `trader/autopilot.json` |
| every 5-min bar, 25 s after it closes, from the end of the opening range to `last_entry` | `scan.py orb` over today's list. A breakout is a candidate only if it is fresh (the bar that just closed) and not extended (`max_pct_over`) |
| per candidate | news (blocks on `news_block_tags` or an important 8-K), entry (`triggered`), exit plan (`stop`, `target`), Time Machine (10,000 replays, seed 19, `--until flat_by`), a fresh quote, budget, risk. Any NO stops the chain and the reason is shown in the dashboard |
| PASS | the approval is written to `trader/state/broker.json` marked `approved_by: autopilot`, the bracket order goes to Alpaca paper, the fill is recorded |
| while open | the bracket's own stop-loss and take-profit do the exits; each fill is recorded with `ticket.py fill` |
| `flat_by` (15:55) | open legs are canceled, `ticket.py flatten` + trader-risk + a market sell for whatever is still held |

## The Time Machine gate

With R the target in R: edge = target-first / (target-first + stop-first) minus the
break-even 1 / (1 + R), in points. A trade needs `edge >= tm_min_edge_pts`, an
average simulated R of at least `tm_min_avg_R`, and, when the same rule fired at
least `tm_hist_min_trades` times in the last 60 days, a historical average R of at
least `tm_hist_min_avg_R`.

## Steps for Claude

1. **Start**: check `.env` exists (never read it aloud, never ask for keys in chat),
   then `./start.sh`, then `autopilot.py status`.
2. **Status / "how did it go"**: read `trader/autopilot/status.json` and `history.json`;
   report trades, P&L in dollars and R, and the reasons candidates were skipped.
   Say plainly when results are negative.
3. **Tune**: run the three tuning scripts, read `tuning/results.json` and
   `tuning/gate_results.json`, and propose changes to `trader/autopilot.json` with
   the in-sample AND out-of-sample numbers. Never pick a setting on in-sample alone.
4. **Pause** when the user asks, or when the dashboard shows repeated errors.

## What it never does

- Never trades a live account: the host is fixed to paper-api.alpaca.markets and the
  account number must start with PA. `"paper": false` in the config stops it.
- Never moves a stop, never averages down, never holds overnight, never shorts.
- Never asks for, prints or stores API keys outside `.env` / the environment.
- Not financial advice. Most day traders lose money; a backtest is not a promise.
