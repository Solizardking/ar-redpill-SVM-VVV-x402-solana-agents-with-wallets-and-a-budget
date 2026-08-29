# Giving an Agent a Wallet and a Budget — Solana/SVM Edition

Pay for inference from a Solana wallet with no API key, and cap what the agent can spend.

Adapted from the Venice x402 guide (EVM/Base edition, by Sabrina Aquino — 21 August 2026). Same protocol, different rail: the agent holds an **Ed25519 keypair**, proves its identity with **Sign In With Solana (SIWS)** signatures, and settles top-ups in **USDC on Solana mainnet-beta**.

This folder is the **wire-format source of truth** for the SOLGPT **X402 Autonomous Desk** (`server/x402-agent/` + `src/components/X402AgentScreen.tsx`). The TypeScript engine is the production port; `agent.py` is the verified reference, and E2B "Give this agent a computer" seeds these files into `/home/user/solgpt/venice-x402-solana`.

`agent.py` runs without a funded wallet and stops at the payment wall, so you can see the whole flow before spending anything.

## How it works

Four moving parts, three of which are just HTTP:

| Step | Endpoint | Why |
|---|---|---|
| Prove who you are | Any, via SIWS header | A signed Ed25519 message replaces the API key |
| Put money in | `/x402/top-up` | Discover the rails, then settle a partially-signed USDC transfer |
| Check the balance | `/x402/balance/{address}` | What is left, and whether it is enough to transact |
| Read the charges | `/x402/transactions/{address}` | Per-request ledger of every debit |

Inference itself is the ordinary `/chat/completions` call. The only difference is which header you send.

The autonomous desk adds a fifth moving part that is not Venice: the **pump.fun launch tape** (`wss://clawd-ws.fly.dev/ws`). Hard filters run first (free). A BUY/SKIP verdict costs ~$0.000007. Execution, when enabled, is a POST to the SOLGPT desk (`/api/x402-agent/buy`) so the encrypted server-side wallet is the one that signs.

## What it costs to start

Two numbers matter and they are not the same number:

- **Minimum top-up: $5.** Returned in the discovery response (`minimumTopUpUsd`) — read it rather than trusting this page.
- **Minimum balance to make a call: $0.10.** A wallet holding less gets a 402 from inference even though it holds money.

At ~$0.000007 per short call, five dollars is on the order of three quarters of a million questions. The budget here is not a tight constraint; it is a blast radius.

## Setup

Python **3.10–3.12** required (`solana>=0.36,<0.38` does not install cleanly on 3.13/3.14). Homebrew `python` on this machine is 3.14, so use a venv:

```bash
cd venice-x402-solana
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python agent.py                 # re-execs .venv automatically
```

`agent.py` will hop into `.venv` if it exists. If packages are missing you get the three commands above instead of a raw `ModuleNotFoundError: base58`.

> `solana>=0.36,<0.38` matters: newer solana-py releases moved `solana.rpc.api`, which breaks x402's SVM mechanism until its extra declares the upper bound.

`websocket-client` is only needed for `--desk` (the launch tape). Status, demo, and top-up run without it.

## The wallet

```python
from solders.keypair import Keypair
from x402.mechanisms.svm import KeypairSigner

signer = KeypairSigner(Keypair())          # throwaway
# signer = KeypairSigner.from_base58(key)  # funded, secret from env/secret manager
```

A wallet with no USDC cannot do anything expensive by accident. Keep the private key out of the notebook; in Colab use Secrets and read it with `userdata.get("AGENT_WALLET_KEY")`.

On the SOLGPT desk, **Export key** copies this same secret so the Python agent and the TypeScript engine can share one wallet. Prefer driving trades through `/api/x402-agent/buy` (`--desk --execute`) so the key stays encrypted at rest on the server.

## Signing in instead of authenticating

Each request carries a base64-encoded JSON payload in the `SIGN-IN-WITH-X` header containing the message, the address, and an **Ed25519 signature over the exact message bytes, base58 encoded**. Venice rebuilds these bytes server-side and verifies against them, so a stray blank line means a rejected signature rather than a helpful error.

Verified working message format:

```
api.venice.ai wants you to sign in with your Solana account:
<base58 pubkey>

Sign in to Venice AI

URI: https://api.venice.ai
Version: 1
Chain ID: solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp
Nonce: <hex>
Issued At: <iso8601>
Expiration Time: <iso8601>
```

The replay rules are identical to the EVM edition: signature valid for five minutes, each nonce single-use for about five and a half minutes, and the signer must match the wallet in the path or you get a 403. Sign a fresh header per request — signing is local and free.

## Putting money in

Top-up discovery returns one entry per rail; today that is Base and Solana:

```json
{
  "scheme": "exact",
  "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
  "amount": "5000000",
  "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "payTo": "8qUL23aSj7mDWdoLMXGHFvnVCT9wd7jXcysiekroADEL",
  "maxTimeoutSeconds": 300,
  "extra": {
    "name": "USD Coin",
    "version": "2",
    "feePayer": "BFK9TLC3edb13K6v4YyH3DwPb5DSUpkWvb7XnqCL9b4F"
  }
}
```

Three Solana-specific details worth walking past slowly:

- `amount` is in base units and USDC has six decimals, so `5000000` is five dollars.
- `extra.feePayer` is a Venice-operated account that covers the transaction fee. The SDK builds a **partially-signed versioned transaction**: your Ed25519 signature at index 1, a zeroed placeholder at index 0 that Venice's facilitator fills before submitting. This is what lets the wallet pay without ever holding SOL.
- `asset` is the native USDC mint on mainnet-beta.

The default spend-control trap exists identically on this rail: the SDK ships with `max_amount_per_payment` set to one dollar, below the $5 minimum top-up, so every rail is rejected with `NoMatchingRequirementsError` before any network contact. Raise it deliberately rather than turning spend controls off:

```python
client.set_spend_controls(SpendControls(max_amount_per_payment="$5", allowed_assets=True))
```

Settling from a wallet with no USDC fails verification — the expected shape of failure: signature fine, transfer not.

## Paying per call

```python
response = requests.post(
    f"{BASE_URL}/chat/completions",
    headers={"SIGN-IN-WITH-X": siws_header(), "Content-Type": "application/json"},
    json={...},
)
```

Handle 402 as a normal outcome rather than an exception. An agent paying its own way runs out of money eventually, and running out of money is not a crash. Turning off the Venice system prompt matters more than it looks: it costs about seventeen hundred input tokens per call.

`--refill` / `AUTO_REFILL=1` catches the 402, settles one top-up, and retries once. That is the second lock next to the session `--budget`.

## Reading what it spent

The ledger is authoritative — ask what was actually charged instead of estimating from token counts:

```json
{
  "id": "ledger_01H...",
  "amount": -0.0000066,
  "balanceAfter": 4.9999934,
  "type": "CHARGE",
  "createdAt": "2026-08-21T18:22:10.000Z",
  "requestId": "chatcmpl-...",
  "modelId": "qwen3-5-9b"
}
```

`TOP_UP` and `REFUND` rows appear too; filter to `CHARGE` for spend. The desk UI reads the same ledger at `GET /api/x402-agent/ledger`.

## X402 autonomous desk

`--desk` is the tool-calling loop the original demo promised: swap the CS-question list for the live launch tape.

```
hard filters (free) → Venice verdict (paid) → optional POST /api/x402-agent/buy
```

Verdict schema matches `server/x402-agent/engine.ts`:

```json
{"decision":"BUY"|"SKIP","confidence":0.0-1.0,"reason":"max 140 chars"}
```

SKIP is the default. A BUY only executes when `--execute` is set, `DESK_JWT` is present, and confidence ≥ `X402_MIN_CONFIDENCE` (default 0.6).

| Env | Default | Same knob on the desk UI |
|---|---|---|
| `X402_TRADE_SIZE_SOL` | `0.05` | Trade size (SOL) |
| `X402_MAX_TRADES_PER_DAY` | `10` | Max trades / day |
| `X402_MIN_CONFIDENCE` | `0.6` | Confidence floor |
| `X402_EVAL_COOLDOWN_SEC` | `20` | Eval cooldown |
| `X402_REQUIRE_SOCIALS` | off | Socials req |
| `X402_REQUIRE_GITHUB` | off | GitHub req |
| `X402_V2_ONLY` | off | V2 only |
| `DESK_API_URL` | `http://127.0.0.1:3000` | desk origin |
| `DESK_JWT` | unset | session Bearer (execute path) |
| `PUMP_RELAY_WS_URL` | `wss://clawd-ws.fly.dev/ws` | launch tape |

`--execute` does **not** sign swaps in Python. It asks the TypeScript engine to buy, so TP/SL, soul autopsies, and the encrypted keystore stay in one place.

## Running it

```bash
python agent.py                 # prints balance, stops at the wall on an empty wallet
python agent.py --demo          # original $5 CS-question budget loop
python agent.py --top-up        # settle a Solana USDC top-up
python agent.py --desk          # autonomous desk: tape → verdicts, no trades
python agent.py --desk --execute --budget 0.01
```

Then fund via `top_up()` / `--top-up` and run the loop:

```python
run()              # $5.00, finishes every task
run(budget=1e-5)   # stops partway, having spent about $0.000007 per call
desk()             # watches the tape until the session budget is gone
```

## Where this leaves you

The agent holds its own money, proves identity with an Ed25519 signature, and cannot exceed a limit you set — all without an account existing anywhere. For scheduled jobs or serverless functions, that is a materially different security posture than handing out a long-lived API key.

What this folder now covers (the original next steps):

- **Cap it in the protocol** — spend controls are per payment (`max_amount_per_payment="$5"`); pair them with `--budget` so a bug in one cannot defeat the other.
- **Refill on empty** — `--refill` / `AUTO_REFILL=1` catches the 402, tops up once, retries.
- **Give it real work** — `--desk` is the tool-calling loop; the ledger shows what each decision cost. `--execute` hands fills to the X402 Autonomous Desk so the production engine remains the signer.
