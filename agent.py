"""
Giving an Agent a Wallet and a Budget — Solana/SVM edition.

The agent holds an Ed25519 keypair, authenticates by signing a message
(SIWS), pays for each inference out of a USDC balance attached to that
Solana address, and reads back every charge from a ledger. No API key,
no account, no dashboard. The ceiling is the balance, plus the budget
the agent enforces on itself below.

Runs without a funded wallet and stops at the payment wall.

    pip install "x402[svm]" "solana>=0.36,<0.38" solders base58 requests

Optional: export AGENT_WALLET_KEY=<base58 secret key> to use a funded
wallet instead of a throwaway.
"""

import base64
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import base58
import requests
from solders.keypair import Keypair

from x402.client import SpendControls, x402ClientSync
from x402.http import PAYMENT_SIGNATURE_HEADER, encode_payment_signature_header
from x402.mechanisms.svm import KeypairSigner
from x402.mechanisms.svm.exact.client import ExactSvmScheme
from x402.schemas.payments import PaymentRequired


BASE_URL = "https://api.venice.ai/api/v1"
DOMAIN = "api.venice.ai"
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"  # mainnet-beta
MODEL = "qwen3-5-9b"
BUDGET_USD = 5.00


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
    return response.json()["data"]


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

def ask(question):
    response = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"SIGN-IN-WITH-X": siws_header(), "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": question}],
            "max_completion_tokens": 150,
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
        body = response.json()
        raise RuntimeError(
            f"balance ${body.get('currentBalanceUsd', 0)} is under the "
            f"${body.get('minimumBalanceUsd')} floor. Minimum top-up is "
            f"${body['topUpInstructions']['minimumAmountUsd']}."
        )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Reading what it spent
# ---------------------------------------------------------------------------

def charges():
    """Every debit against this wallet, newest first."""
    ledger = wallet_get(f"/x402/transactions/{signer.address}", limit=100)
    return [t for t in ledger["transactions"] if t["type"] == "CHARGE"]


# ---------------------------------------------------------------------------
# The budgeted run
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


if __name__ == "__main__":
    print(balance())
