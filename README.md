# Giving an Agent a Wallet and a Budget — Solana/SVM Edition

Pay for inference from a Solana wallet with no API key, restrict each payment, and track a session budget from the provider ledger.

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

At ~$0.000007 per short call, five dollars is on the order of three quarters of a million questions. The session budget is checked between calls, so it can overrun by at most the next inference charge; the separate $5 x402 control limits one top-up payment.

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

On the SOLGPT desk, **Export key** copies this same secret so the Python agent and the TypeScript engine can share one wallet. Execution is refused unless the desk snapshot reports the exact same public key as `AGENT_WALLET_KEY`. Prefer driving trades through `/api/x402-agent/buy` (`--desk --execute`) so the key stays encrypted at rest on the server.

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

The default spend-control trap exists identically on this rail: the SDK ships with `max_amount_per_payment` set to one dollar, below the $5 minimum top-up, so every rail is rejected with `NoMatchingRequirementsError` before any network contact. Raise only that one-payment ceiling; omitting `allowed_assets` preserves the SDK's stablecoin allowlist:

```python
client.set_spend_controls(SpendControls(max_amount_per_payment="$5"))
```

Before signing, `agent.py` independently requires the `exact` scheme, the configured Solana mainnet CAIP-2 network, native mainnet USDC, an integer amount no greater than 5,000,000 base units, and an advertised `feePayer`. It narrows the payment requirements to that single rail. Set `VENICE_X402_PAY_TO` to Venice's expected recipient address if you also want a deployment-specific payee pin.

Discovery must return HTTP 402, and discovery, settlement, SIWS, and desk requests do not follow redirects. Settling from a wallet with no USDC fails verification — the expected shape of failure: signature fine, transfer not.

## Paying per call

```python
response = requests.post(
    f"{BASE_URL}/chat/completions",
    headers={"SIGN-IN-WITH-X": siws_header(), "Content-Type": "application/json"},
    json={...},
)
```

Handle 402 as a normal outcome rather than an exception. An agent paying its own way runs out of money eventually, and running out of money is not a crash. Turning off the Venice system prompt matters more than it looks: it costs about seventeen hundred input tokens per call.

`--refill` / `AUTO_REFILL=1` catches the 402, settles at most one top-up, and retries the inference request once. A second 402 stops instead of recursively topping up.

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

`TOP_UP` and `REFUND` rows appear too; filter to `CHARGE` for spend. The tracker remembers stable charge IDs and accumulates newly observed rows, so rotation of the newest-100 ledger window cannot reset the session total. After each paid call, the process stops if its charge does not appear in the ledger. The desk UI reads the same ledger at `GET /api/x402-agent/ledger`.

## X402 autonomous desk

`--desk` is the tool-calling loop the original demo promised: swap the CS-question list for the live launch tape.

```
hard filters (free) → Venice verdict (paid) → optional POST /api/x402-agent/buy
```

Verdict schema matches `server/x402-agent/engine.ts`:

```json
{"decision":"BUY"|"SKIP","confidence":0.0-1.0,"reason":"max 140 chars"}
```

SKIP is the default. A BUY only executes when `--execute` is set, `DESK_JWT` is present, confidence ≥ `X402_MIN_CONFIDENCE` (default 0.6), and the live desk snapshot passes wallet, trade-cap, and SOL-reserve checks. The desk must use HTTPS unless it is a loopback URL; redirects are rejected so a Bearer token is never forwarded to another origin.

| Env | Default | Same knob on the desk UI |
|---|---|---|
| `X402_TRADE_SIZE_SOL` | `0.05` | Trade size (SOL) |
| `X402_MAX_TRADES_PER_DAY` | `10` | Max trades / day |
| `X402_MIN_CONFIDENCE` | `0.6` | Confidence floor |
| `X402_EVAL_COOLDOWN_SEC` | `20` | Eval cooldown |
| `X402_RESERVE_SOL` | `0.01` | SOL kept after trade, before fee buffer |
| `X402_REQUIRE_SOCIALS` | off | Socials req |
| `X402_REQUIRE_GITHUB` | off | GitHub req |
| `X402_V2_ONLY` | off | V2 only |
| `DESK_API_URL` | `http://127.0.0.1:3000` | desk origin |
| `DESK_JWT` | unset | session Bearer (execute path) |
| `PUMP_RELAY_WS_URL` | `wss://clawd-ws.fly.dev/ws` | launch tape |

Top-up-specific safety knobs:

| Env | Default | Meaning |
|---|---|---|
| `BUDGET_USD` | `5.00` | session inference threshold; finite and non-negative |
| `AUTO_REFILL` | off | permit one top-up and one retry after an inference 402 |
| `VENICE_X402_PAY_TO` | unset | optional exact recipient address required during top-up discovery |

`--execute` does **not** sign swaps in Python. It asks the TypeScript engine to buy, so TP/SL, soul autopsies, and the encrypted keystore stay in one place.

## SAID identity

The same wallet is a SAID agent. Off-chain registration is free; the trust screen is an x402 resource (0.001 USDC on Solana mainnet USDC). `SAID_API_KEY` is sent as `X-API-Key` on every SAID request when set.

```bash
SAID_API_KEY=… python agent.py --said-register
python agent.py --said-screen COUNTERPARTY_WALLET          # stops at the 402 wall
python agent.py --said-screen COUNTERPARTY_WALLET --pay-screen
python agent.py --desk                                     # registers, then screens creators
```

`--desk` registers the inference wallet at `POST /api/register/pending`, then skips launches whose creator SAID screen returns `caution`. An unpaid 402 is not a skip — screening is best-effort until the wallet can pay.

The relay payload is untrusted. A launch must contain a valid Solana mint, repeated mints are deduplicated, and the model is explicitly told never to treat token metadata as instructions. A desk response counts as a fill only when it has `ok: true` and a transaction signature.

## Pump program scope

This reference client is an inference payer and an optional caller of the existing SOLGPT desk endpoints. It is **not** a direct Pump or PumpSwap SDK, does not load the Pump IDLs, and does not claim that the generic `/api/x402-agent/buy` and `/sell` routes are Pump `buy_v2` or `sell_v2` instructions.

In particular, this folder does not yet build or decode `create_v2`, `buy_v2`, `sell_v2`, `claim_cashback_v2`, creator-fee collection, creator-fee sharing, or PumpSwap liquidity instructions. Those capabilities need explicit public desk endpoints or audited local transaction builders first; adding similarly named HTTP calls here would not implement the documented account layouts, PDAs, token programs, fee recipients, quote-mint rules, or slippage semantics. The live x402 and desk rails in this example are mainnet-only; use a separate devnet implementation and wallet for launchpad integration testing.

## Running it

```bash
python agent.py                 # prints balance, stops at the wall on an empty wallet
python agent.py --demo          # original $5 CS-question budget loop
python agent.py --top-up        # settle a Solana USDC top-up
python agent.py --desk          # autonomous desk: tape → verdicts, no trades
python agent.py --desk --execute --budget 0.01
SAID_API_KEY=… python agent.py --said-register
python agent.py --said-screen COUNTERPARTY_WALLET
```

Then fund via `top_up()` / `--top-up` and run the loop:

```python
run()              # $5.00, finishes every task
run(budget=1e-5)   # stops partway, having spent about $0.000007 per call
desk()             # watches the tape until the session budget is gone
```

Run the offline safety regressions without creating bytecode artifacts:

```bash
.venv/bin/python -B -m unittest -v test_agent.py
```

## Where this leaves you

The agent holds its own money, proves identity with an Ed25519 signature, restricts top-up rails, and accounts for session spend from authoritative charge IDs — all without a long-lived API key. The session threshold is checked between calls rather than enforced by the payment protocol, so size it with one-call headroom and keep wallet balances limited.

What this folder now covers (the original next steps):

- **Cap each x402 payment** — spend controls are per payment (`max_amount_per_payment="$5"`) and the code separately pins exact mainnet USDC; pair them with `--budget` and a deliberately limited wallet balance.
- **Refill on empty** — `--refill` / `AUTO_REFILL=1` catches the 402, tops up once, retries.
- **Give it real work** — `--desk` is the tool-calling loop; the ledger shows what each decision cost. `--execute` hands fills to the X402 Autonomous Desk so the production engine remains the signer.
