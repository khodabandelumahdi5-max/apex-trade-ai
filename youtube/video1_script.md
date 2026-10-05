# Video 1 — "I Built an AI Trading Bot. Here Are the Honest Results."

Length: ~8 minutes · Format: screen recording (no face) + voice-over
Record the clips with `record.bat live 5` and `record.bat timelapse 24`, then put this voice-over on top.
راهنمای فارسی هر صحنه داخل [کروشه] آمده است؛ فقط متن انگلیسی را بخوانید یا به صدای هوش مصنوعی بدهید.

---

## 0:00 – 0:30 · Hook
[صحنه: تایم‌لپس ۲۴ ساعته‌ی داشبورد، سریع]

> I built a trading bot with four AI agents. It watches whales on the Solana blockchain, reads the charts,
> manages risk, and places trades — completely on its own.
> Most videos like this show you fake profits. This one shows you the real numbers. Including the bad ones.

**On-screen text:** PAPER TRADING · NO REAL MONEY · NOT FINANCIAL ADVICE

## 0:30 – 2:00 · How it works
[صحنه: بالای داشبورد؛ روی چهار کارت عامل‌ها مکث کنید]

> The bot is a swarm of four agents.
> The **technical agent** reads one-hour and four-hour charts — moving averages, RSI, volatility —
> and turns them into a confidence score between zero and one.
> The **on-chain agent** tracks the biggest wallets on Solana. If whales are dumping, it can veto a trade.
> The **risk agent** decides the size. It never risks more than one percent of the account on a single trade,
> and it stops trading completely if the account drops ten percent.
> The **execution agent** places the orders. Right now on MEXC prices — in paper mode, so no real money.

## 2:00 – 4:30 · A live trade
[صحنه: جدول Live positions و Trade plan؛ اگر ضبطی دارید که Breakeven Locked می‌شود، اینجا بگذارید]

> Here's a real trade from this week. JUP — the bot bought at thirty-four and a half cents.
> Every trade has a plan before it opens: a stop loss, a first target, and a second target.
> At the first target it sells half and takes the profit.
> And here's my favourite part: once the trade is up one and a half percent,
> the bot moves the stop loss above the entry price.
> See this — "Breakeven Locked". Risk left: zero. From here, this trade cannot lose money.
> After that the stop trails the price, so if it keeps going up, the stop follows it.

## 4:30 – 5:30 · Same strategy on TradingView
[صحنه: چارت TradingView با برچسب BUY و خط‌های حد ضرر و تارگت]

> I also rebuilt the exact same rules in TradingView, so you can see the signals on a normal chart.
> Green label: buy signal. Red line: stop. The two green lines: the targets.

## 5:30 – 7:00 · The honest results
[صحنه: جدول بک‌تست از README یا عکس صفحه‌ی Strategy Tester]

> Now the part most channels skip. I backtested this bot on a full year of real data.
> That year Solana fell more than twenty percent, and at its worst the market was down over sixty percent.
> The bot? Its worst drawdown was under four percent. The risk management did its job.
> But — it didn't make money. It lost about one to two percent over the year.
> It only found three to seven trades the whole year, and most of them didn't win.
> So: great at protecting money. Not yet good at making it.

## 7:00 – 8:00 · What's next
[صحنه: داشبورد زنده]

> In the next videos I'm going to try to fix that — test new strategies, show every result,
> and only move to real money if the numbers actually say yes.
> If you want to see whether an AI bot can really beat the market — honestly — subscribe and follow along.

**On-screen text (end card):** Paper trading only · Not financial advice · Past results don't predict the future

---

## Description (paste into YouTube)

I built a multi-agent AI trading bot (technical analysis, on-chain whale tracking, risk management, automatic
execution) and tested it honestly — including a full one-year backtest.

⚠️ Paper trading only. No real money. This is not financial advice. Trading crypto can lose you money.

Chapters:
0:00 The bot
0:30 How the 4 agents work
2:00 A live trade (breakeven lock)
4:30 Same strategy on TradingView
5:30 Honest backtest results
7:00 What's next

## Before you publish — checklist
- [ ] Watched the whole video: no `.env`, keys, file paths, user name, e-mail, personal tabs
- [ ] "Paper trading / not financial advice" visible at the start and the end
- [ ] No profit promises anywhere (title, thumbnail, description, comments)
- [ ] Every number in the voice-over matches what is on screen in your recording
