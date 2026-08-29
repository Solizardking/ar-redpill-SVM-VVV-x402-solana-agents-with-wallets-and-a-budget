"""
Giving an Agent a Wallet and a Budget — Solana/SVM edition.

The agent holds an Ed25519 keypair, authenticates by signing a message
(SIWS), pays for each inference out of a USDC balance attached to that
Solana address, and reads back every charge from a ledger. No API key,
no account, no dashboard. The ceiling is the balance, plus the budget
the agent enforces on itself below.

Runs without a funded wallet and stops at the payment wall.

    pip install "x402[svm]" "solana>=0.36,<0.38" solders base58 requests websocket-client

Optional env:

    AGENT_WALLET_KEY     base58 secret key (funded wallet; else throwaway)
    DESK_API_URL         SOLGPT x402 autonomous desk origin (default localhost)
    DESK_JWT             Bearer JWT for /api/x402-agent/* execute path
    PUMP_RELAY_WS_URL    launch tape (default wss://clawd-ws.fly.dev/ws)
    BUDGET_USD           session inference cap (default 5.00)
    AUTO_REFILL          1 = catch 402, top up once, retry
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

# Homebrew `python` is 3.14 on this machine; x402[svm] + solana<0.38 need 3.10–3.12.
# If a local .venv exists, re-exec so `python agent.py` works without activating.
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_PY = os.path.join(_HERE, ".venv", "bin", "python")
if os.path.isfile(_VENV_PY) and os.path.abspath(sys.executable) != os.path.abspath(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

try:
    import base58
    import requests
    from solders.keypair import Keypair

    from x402.client import SpendControls, x402ClientSync
    from x402.http import PAYMENT_SIGNATURE_HEADER, encode_payment_signature_header
    from x402.mechanisms.svm import KeypairSigner
    from x402.mechanisms.svm.exact.client import ExactSvmScheme
    from x402.schemas.payments import PaymentRequired
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"missing {exc.name!r}. This agent needs Python 3.10–3.12 and its venv packages:\n"
        f"  python3.12 -m venv {_HERE}/.venv\n"
        f"  {_HERE}/.venv/bin/python -m pip install -r {_HERE}/requirements.txt\n"
        f"  {_HERE}/.venv/bin/python agent.py"
    ) from exc


BASE_URL = "https://api.venice.ai/api/v1"
DOMAIN = "api.venice.ai"
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"  # mainnet-beta
MODEL = os.environ.get("VENICE_MODEL", "qwen3-5-9b")
BUDGET_USD = float(os.environ.get("BUDGET_USD", "5.00"))
PUMP_RELAY_WS_URL = (
    os.environ.get("PUMP_RELAY_WS_URL")
    or os.environ.get("NEXT_PUBLIC_CLAWD_WS_URL")
    or "wss://clawd-ws.fly.dev/ws"
)
DESK_API_URL = os.environ.get("DESK_API_URL", "http://127.0.0.1:3000").rstrip("/")
AUTO_REFILL = os.environ.get("AUTO_REFILL", "").strip() in {"1", "true", "TRUE", "yes"}

# Hard filters — same defaults as server/x402-agent/engine.ts
TRADE_SIZE_SOL = float(os.environ.get("X402_TRADE_SIZE_SOL", "0.05"))
MAX_TRADES_PER_DAY = int(os.environ.get("X402_MAX_TRADES_PER_DAY", "10"))
MIN_CONFIDENCE = float(os.environ.get("X402_MIN_CONFIDENCE", "0.6"))
REQUIRE_SOCIALS = os.environ.get("X402_REQUIRE_SOCIALS", "").strip() in {"1", "true", "TRUE"}
REQUIRE_GITHUB = os.environ.get("X402_REQUIRE_GITHUB", "").strip() in {"1", "true", "TRUE"}
V2_ONLY = os.environ.get("X402_V2_ONLY", "").strip() in {"1", "true", "TRUE"}
EVAL_COOLDOWN_SEC = int(os.environ.get("X402_EVAL_COOLDOWN_SEC", "20"))
RESERVE_SOL = float(os.environ.get("X402_RESERVE_SOL", "0.01"))

DESK_SYSTEM = (
    "You are a ruthless pump.fun sniper deciding buys in under a second. "
    'Reply with STRICT JSON only: {"decision":"BUY"|"SKIP","confidence":0.0-1.0,"reason":"max 140 chars"}. '
    "SKIP is the default. Buy signals: coherent narrative, real socials/github, sane mcap. "
    "Hard skips: copy-paste spam, gibberish names, no links, honeypot vibes."
)


# ---------------------------------------------------------------------------
# A wallet the agent owns
# ---------------------------------------------------------------------------

# In production this is a keypair you funded deliberately and whose secret
# lives in a secret manager. While building, generating a throwaway is the
# right move: a wallet with no USDC cannot do anything expensive by accident.
_key = os.environ.get("AGENT_WALLET_KEY")
signer = KeypairSigner.from_base58(_key) if _key else KeypairSigner(Keypair())

print(f"wallet {signer.address}")
print("funded" if _key else "disposable, cannot pay yet")


class InferencePaymentWall(RuntimeError):
    """402 from /chat/completions — running out of money is not a crash."""

    def __init__(self, current: float, minimum: float, top_up: float):
        self.current_balance_usd = current
        self.minimum_balance_usd = minimum
        self.minimum_top_up_usd = top_up
        super().__init__(
            f"balance ${current} is under the ${minimum} floor. "
            f"Minimum top-up is ${top_up}."
        )


# ---------------------------------------------------------------------------
# Signing in instead of authenticating (SIWS)
# ---------------------------------------------------------------------------

def siws_header():
    """Fresh signed header per request; signing is local and free."""
    now = datetime.now(timezone.utc)
    stamp = lambda t: t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    issued_at, expires_at = stamp(now), stamp(now + timedelta(minutes=4))
    nonce = secrets.token_hex(8)

    message = (
        f"{DOMAIN} wants you to sign in with your Solana account:\n"
        f"{signer.address}\n\nSign in to Venice AI\n\n"
        f"URI: https://{DOMAIN}\nVersion: 1\nChain ID: {NETWORK}\n"
        f"Nonce: {nonce}\nIssued At: {issued_at}\nExpiration Time: {expires_at}"
    )
    # Ed25519 signature over the exact message bytes, base58 encoded.
    signature = base58.b58encode(bytes(signer.keypair.sign_message(message.encode()))).decode()

    payload = {
        "address": signer.address,
        "message": message,
        "signature": signature,
        "chainId": NETWORK,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def wallet_get(path, **params):
    response = requests.get(
        f"{BASE_URL}{path}",
        headers={"SIGN-IN-WITH-X": siws_header()},
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def balance():
    return wallet_get(f"/x402/balance/{signer.address}")


# ---------------------------------------------------------------------------
# Putting money in (top-up via the Solana rail)
# ---------------------------------------------------------------------------

def top_up():
    """Two requests: unauthenticated discovery, then a signed transfer."""
    discovery = requests.post(f"{BASE_URL}/x402/top-up", timeout=30)
    required = PaymentRequired.model_validate(discovery.json())

    rail = next(a for a in required.accepts if a.network.startswith("solana"))
    print(
        f"{rail.network}: {int(rail.amount) / 1e6:.2f} USDC to {rail.pay_to}, "
        f"feePayer {rail.extra['feePayer']}"
    )

    client = x402ClientSync()
    client.register(rail.network, ExactSvmScheme(signer))
    # The SDK caps one payment at $1 by default, which is below the $5 minimum
    # top-up, so every rail gets rejected until this is raised.
    client.set_spend_controls(SpendControls(max_amount_per_payment="$5", allowed_assets=True))

    # Builds a partially-signed SPL TransferChecked transaction: the agent's
    # signature sits at index 1 and Venice's feePayer co-signs at index 0
    # before submission, which is why the wallet needs no SOL.
    payload = client.create_payment_payload(required)

    settlement = requests.post(
        f"{BASE_URL}/x402/top-up",
        headers={PAYMENT_SIGNATURE_HEADER: encode_payment_signature_header(payload)},
        timeout=90,
    )
    if settlement.status_code != 200:
        raise RuntimeError(
            f"settlement failed ({settlement.status_code}): {settlement.json()}. "
            "From an empty wallet this is expected — the signature was fine "
            "and the transfer was not."
        )
    return settlement.json()


# ---------------------------------------------------------------------------
# Paying per call
# ---------------------------------------------------------------------------

def chat(messages, *, max_tokens=150, refill=False):
    """One inference call. 402 is a normal outcome, not a crash."""
    response = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"SIGN-IN-WITH-X": siws_header(), "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "venice_parameters": {
                # The default Venice system prompt costs ~1700 input tokens
                # per call — two orders of magnitude more than the question.
                "include_venice_system_prompt": False,
                "disable_thinking": True,
            },
        },
        timeout=90,
    )
    if response.status_code == 402:
        body = response.json() if response.content else {}
        wall = InferencePaymentWall(
            float(body.get("currentBalanceUsd", 0) or 0),
            float(body.get("minimumBalanceUsd") or 0.10),
            float((body.get("topUpInstructions") or {}).get("minimumAmountUsd") or 5),
        )
        if refill or AUTO_REFILL:
            print(f"> 402 — brain empty (${wall.current_balance_usd:.4f}). topping up once.")
            top_up()
            return chat(messages, max_tokens=max_tokens, refill=False)
        raise wall
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def ask(question):
    return chat([{"role": "user", "content": question}])


# ---------------------------------------------------------------------------
# Reading what it spent
# ---------------------------------------------------------------------------

def charges():
    """Every debit against this wallet, newest first."""
    ledger = wallet_get(f"/x402/transactions/{signer.address}", limit=100)
    if isinstance(ledger, list):
        txs = ledger
    elif isinstance(ledger, dict):
        txs = ledger.get("transactions") or []
    else:
        txs = []
    return [t for t in txs if isinstance(t, dict) and t.get("type") == "CHARGE"]


def spent_since(baseline: float) -> float:
    return sum(abs(c.get("amount") or 0) for c in charges()) - baseline


# ---------------------------------------------------------------------------
# The budgeted run (original demo — short CS questions)
# ---------------------------------------------------------------------------

TASKS = [
    "Name one concrete tradeoff of vector search versus keyword search. One sentence.",
    "In one sentence, when is a bloom filter the wrong choice?",
    "Give one reason CRDTs are hard to debug in production. One sentence.",
    "What is one failure mode of exponential backoff without jitter? One sentence.",
    "Name one thing consistent hashing does not solve. One sentence.",
    "Why is p99 latency more useful than the mean? One sentence.",
]


def run(budget=BUDGET_USD):
    opening = balance()
    print(f"balance ${opening['balanceUsd']:.4f}, budget ${budget:.4f}")

    if not opening["canConsume"]:
        print(f"cannot transact yet, minimum top-up is ${opening['minimumTopUpUsd']}")
        return

    baseline = sum(abs(c["amount"]) for c in charges())
    spent = 0.0

    for number, task in enumerate(TASKS, 1):
        if spent >= budget:
            print(f"\nstopped before task {number}: ${spent:.6f} of ${budget:.4f} spent")
            return

        answer = ask(task)
        spent = sum(abs(c["amount"]) for c in charges()) - baseline
        print(f"\n{number}. {task}")
        print(f"   {answer}")
        print(f"   ${spent:.6f} spent, ${budget - spent:.6f} left")

    print(f"\nfinished all {len(TASKS)} tasks for ${spent:.6f}")
    if spent:
        print(f"at this rate ${budget:.2f} covers about {int(budget / (spent / len(TASKS))):,} calls")


# ---------------------------------------------------------------------------
# X402 autonomous desk — same brain the TypeScript engine uses
# ---------------------------------------------------------------------------

def parse_verdict(content: str):
    try:
        match_start = content.find("{")
        match_end = content.rfind("}")
        if match_start < 0 or match_end <= match_start:
            return None
        parsed = json.loads(content[match_start : match_end + 1])
        decision = str(parsed.get("decision") or "").upper()
        if decision not in {"BUY", "SKIP"}:
            return None
        return {
            "decision": decision,
            "confidence": min(1.0, max(0.0, float(parsed.get("confidence") or 0))),
            "reason": str(parsed.get("reason") or "")[:160],
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def launch_facts(launch: dict) -> dict:
    t = launch.get("time")
    age = None
    if t:
        try:
            age = round((time.time() * 1000 - datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000) / 1000)
        except ValueError:
            age = None
    return {
        "name": launch.get("name"),
        "ticker": launch.get("symbol"),
        "mint": launch.get("mint"),
        "marketCapSol": launch.get("marketCapSol"),
        "ageSeconds": age,
        "hasTwitter": bool(launch.get("twitter")),
        "hasWebsite": bool(launch.get("website")),
        "hasTelegram": bool(launch.get("telegram")),
        "hasGithub": bool(launch.get("hasGithub")),
        "description": str(launch.get("description") or "")[:240],
        "isV2": bool(launch.get("isV2")),
    }


def hard_filters(launch: dict, trades_today: int) -> str | None:
    """Return a skip reason, or None if the launch may spend a microdollar of thought."""
    if trades_today >= MAX_TRADES_PER_DAY:
        return f"daily cap {MAX_TRADES_PER_DAY}"
    if V2_ONLY and not launch.get("isV2"):
        return "v2-only"
    if REQUIRE_SOCIALS and not (launch.get("twitter") or launch.get("website") or launch.get("telegram")):
        return "socials required"
    if REQUIRE_GITHUB and not launch.get("hasGithub"):
        return "github required"
    if not (launch.get("mint") or launch.get("signature")):
        return "no mint"
    return None


def evaluate_launch(launch: dict, lessons: list[str] | None = None):
    system = DESK_SYSTEM
    if lessons:
        system += "\n\nYOUR SOUL — hard-won rules from your own closed trades. Obey them:\n" + "\n".join(
            f"- {line}" for line in lessons[:10]
        )
    content = chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(launch_facts(launch))},
        ],
        max_tokens=220,
    )
    return parse_verdict(content), content


class DeskAPI:
    """Optional execute rail — the SOLGPT x402 autonomous desk HTTP API."""

    def __init__(self, base: str = DESK_API_URL, jwt: str | None = None):
        self.base = (base or DESK_API_URL).rstrip("/")
        self.jwt = jwt or os.environ.get("DESK_JWT") or ""

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        return headers

    def snapshot(self):
        res = requests.get(f"{self.base}/api/x402-agent/snapshot", headers=self._headers(), timeout=30)
        res.raise_for_status()
        return res.json()

    def buy(self, mint: str, amount_sol: float, symbol: str | None = None):
        res = requests.post(
            f"{self.base}/api/x402-agent/buy",
            headers=self._headers(),
            json={"mint": mint, "amountSol": amount_sol, "symbol": symbol},
            timeout=60,
        )
        res.raise_for_status()
        return res.json()

    def sell(self, mint: str):
        res = requests.post(
            f"{self.base}/api/x402-agent/sell",
            headers=self._headers(),
            json={"mint": mint},
            timeout=60,
        )
        res.raise_for_status()
        return res.json()

    def set_autopilot(self, enabled: bool, config: dict | None = None):
        res = requests.post(
            f"{self.base}/api/x402-agent/autopilot",
            headers=self._headers(),
            json={"enabled": enabled, "config": config or {}},
            timeout=30,
        )
        res.raise_for_status()
        return res.json()


def iter_launches(url: str = PUMP_RELAY_WS_URL):
    """Yield token-launch frames from the SOLGPT pump relay."""
    try:
        import websocket  # websocket-client
    except ImportError as err:
        raise SystemExit(
            "desk mode needs websocket-client — pip install websocket-client"
        ) from err

    retry = 0.25
    while True:
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=30)
            print(f"> tape connected {url}")
            retry = 0.25
            while True:
                raw = ws.recv()
                try:
                    msg = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "token-launch":
                    continue
                if not (msg.get("mint") or msg.get("signature")):
                    continue
                yield msg
        except KeyboardInterrupt:
            raise
        except Exception as err:
            print(f"> tape reconnect in {retry:.1f}s ({err})")
            time.sleep(retry)
            retry = min(retry * 2, 10)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


def desk(budget=BUDGET_USD, execute: bool = False, refill: bool = False):
    """Watch the pump tape, pay Venice per verdict, optionally fire buys on the desk."""
    opening = balance()
    print(f"> X402 AUTONOMOUS DESK")
    print(f"> wallet {signer.address}")
    print(f"> brain ${opening.get('balanceUsd', 0):.4f}  canConsume={opening.get('canConsume')}  budget ${budget:.4f}")
    print(
        f"> filters size {TRADE_SIZE_SOL} SOL · cap {MAX_TRADES_PER_DAY}/day · "
        f"conf ≥ {MIN_CONFIDENCE:.0%} · cooldown {EVAL_COOLDOWN_SEC}s · reserve {RESERVE_SOL} SOL"
    )
    if execute:
        print(f"> execute ON → {DESK_API_URL}/api/x402-agent/buy")
    else:
        print("> execute OFF — verdicts only (pass --execute + DESK_JWT to trade)")

    if not opening.get("canConsume"):
        print(f"> cannot transact yet, minimum top-up is ${opening.get('minimumTopUpUsd', 5)}")
        if refill or AUTO_REFILL:
            print("> AUTO_REFILL — attempting x402 top-up")
            print(top_up())
        else:
            return

    api = DeskAPI() if execute else None
    if api and not api.jwt:
        print("> DESK_JWT missing — cannot execute; falling back to verdicts only")
        api = None

    lessons: list[str] = []
    if api:
        try:
            snap = api.snapshot()
            lessons = list((snap.get("evolution") or {}).get("lessons") or [])
            print(
                f"> desk snapshot autopilot={'on' if (snap.get('autopilot') or {}).get('enabled') else 'off'} "
                f"positions={len(snap.get('positions') or [])} lessons={len(lessons)}"
            )
        except Exception as err:
            print(f"> desk snapshot failed ({err}) — continuing tape-only")

    baseline = sum(abs(c.get("amount") or 0) for c in charges())
    spent = 0.0
    trades_today = 0
    last_eval_at = 0.0
    evaluated = 0

    for launch in iter_launches():
        if spent >= budget:
            print(f"> stopped: ${spent:.6f} of ${budget:.4f} inference spent")
            return

        reason = hard_filters(launch, trades_today)
        if reason:
            continue

        now = time.time()
        if now - last_eval_at < EVAL_COOLDOWN_SEC:
            continue
        last_eval_at = now

        symbol = launch.get("symbol") or (launch.get("mint") or "?")[:6]
        print(f"> eval ${symbol} mint {(launch.get('mint') or '')[:8]}…")
        try:
            verdict, raw = evaluate_launch(launch, lessons)
        except InferencePaymentWall as wall:
            print(f"> brain offline — {wall}")
            return

        evaluated += 1
        spent = spent_since(baseline)
        if not verdict:
            print(f"   unparseable: {raw[:120]}")
            continue

        print(
            f"   {verdict['decision']} ({verdict['confidence'] * 100:.0f}%) "
            f"· {verdict['reason']} · ${spent:.6f} spent"
        )

        buy = (
            verdict["decision"] == "BUY"
            and verdict["confidence"] >= MIN_CONFIDENCE
            and api is not None
            and launch.get("mint")
        )
        if not buy:
            continue

        try:
            out = api.buy(str(launch["mint"]), TRADE_SIZE_SOL, launch.get("symbol"))
        except Exception as err:
            print(f"   buy failed: {err}")
            continue
        trades_today += 1
        sig = (out.get("signature") or "")[:16]
        print(f"   BOUGHT {TRADE_SIZE_SOL} SOL · {sig}{'…' if sig else out}")


def print_status():
    opening = balance()
    print(json.dumps(opening, indent=2))
    if not opening.get("canConsume"):
        print(f"cannot transact yet, minimum top-up is ${opening.get('minimumTopUpUsd')}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Venice x402 Solana agent — wallet, budget, and the X402 autonomous desk.",
    )
    parser.add_argument("--demo", action="store_true", help="original budgeted CS-question loop")
    parser.add_argument("--desk", action="store_true", help="watch the pump tape and issue BUY/SKIP verdicts")
    parser.add_argument("--execute", action="store_true", help="with --desk, POST buys to the SOLGPT desk API")
    parser.add_argument("--top-up", action="store_true", dest="top_up", help="settle an x402 USDC top-up")
    parser.add_argument("--refill", action="store_true", help="on 402, top up once and retry")
    parser.add_argument("--budget", type=float, default=BUDGET_USD, help="session inference cap in USD")
    parser.add_argument("--status", action="store_true", help="print Venice balance JSON (default)")
    args = parser.parse_args(argv)

    global AUTO_REFILL
    if args.refill:
        AUTO_REFILL = True

    if args.top_up:
        print(top_up())
        return
    if args.demo:
        run(budget=args.budget)
        return
    if args.desk:
        desk(budget=args.budget, execute=args.execute, refill=args.refill)
        return
    print_status()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
