"""Offline regression tests for the Venice x402 Solana reference agent."""

from __future__ import annotations

import io
import math
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

with redirect_stdout(io.StringIO()):
    import agent


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"json" if body is not None else b""

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise agent.requests.HTTPError(f"HTTP {self.status_code}")


def payment_required(*, network=agent.NETWORK, asset=agent.USDC_MINT, amount="5000000"):
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": f"{agent.BASE_URL}/x402/top-up",
            "description": "Top up Venice inference credits",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "amount": amount,
                "asset": asset,
                "payTo": "8qUL23aSj7mDWdoLMXGHFvnVCT9wd7jXcysiekroADEL",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "feePayer": "BFK9TLC3edb13K6v4YyH3DwPb5DSUpkWvb7XnqCL9b4F"
                },
            }
        ],
    }


class RefillTests(unittest.TestCase):
    def test_auto_refill_is_attempted_only_once(self):
        responses = [FakeResponse(402, {}), FakeResponse(402, {})]
        with (
            patch.object(agent.requests, "post", side_effect=responses) as post,
            patch.object(agent, "top_up", return_value={"ok": True}) as top_up,
            patch.object(agent, "AUTO_REFILL", True),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(agent.InferencePaymentWall):
                agent.chat([{"role": "user", "content": "test"}])

        self.assertEqual(post.call_count, 2)
        top_up.assert_called_once_with()


class TopUpPolicyTests(unittest.TestCase):
    def test_rejects_non_usdc_and_non_mainnet_rails(self):
        wrong_asset = agent.PaymentRequired.model_validate(
            payment_required(asset="11111111111111111111111111111111")
        )
        with self.assertRaisesRegex(RuntimeError, "exact USDC"):
            agent._top_up_rail(wrong_asset)

        wrong_network = agent.PaymentRequired.model_validate(
            payment_required(network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
        )
        with self.assertRaisesRegex(RuntimeError, "exact USDC"):
            agent._top_up_rail(wrong_network)

    def test_rejects_a_top_up_above_the_explicit_cap(self):
        required = agent.PaymentRequired.model_validate(payment_required(amount="5000001"))
        with self.assertRaisesRegex(RuntimeError, "cap is 5000000"):
            agent._top_up_rail(required)

    def test_optional_payee_pin_is_enforced(self):
        required = agent.PaymentRequired.model_validate(payment_required())
        with patch.object(agent, "EXPECTED_TOP_UP_PAY_TO", "11111111111111111111111111111111"):
            with self.assertRaisesRegex(RuntimeError, "VENICE_X402_PAY_TO"):
                agent._top_up_rail(required)

    def test_top_up_preserves_default_asset_allowlist_and_narrows_accepts(self):
        class FakeClient:
            def __init__(self):
                self.controls = None
                self.registered = None
                self.required = None

            def register(self, network, scheme):
                self.registered = (network, scheme)
                return self

            def set_spend_controls(self, controls):
                self.controls = controls
                return self

            def create_payment_payload(self, required):
                self.required = required
                return object()

        client = FakeClient()
        responses = [
            FakeResponse(402, payment_required()),
            FakeResponse(200, {"success": True}),
        ]
        with (
            patch.object(agent.requests, "post", side_effect=responses),
            patch.object(agent, "x402ClientSync", return_value=client),
            patch.object(agent, "ExactSvmScheme", return_value=object()),
            patch.object(agent, "encode_payment_signature_header", return_value="encoded"),
            patch.object(agent, "EXPECTED_TOP_UP_PAY_TO", ""),
            redirect_stdout(io.StringIO()),
        ):
            result = agent.top_up()

        self.assertEqual(result, {"success": True})
        self.assertEqual(client.registered[0], agent.NETWORK)
        self.assertEqual(client.controls, {"max_amount_per_payment": "$5"})
        self.assertNotIn("allowed_assets", client.controls)
        self.assertEqual(len(client.required.accepts), 1)
        self.assertEqual(client.required.accepts[0].asset, agent.USDC_MINT)


class BudgetTests(unittest.TestCase):
    @staticmethod
    def rows(start: int, stop: int):
        return [
            {"id": f"charge-{i}", "type": "CHARGE", "amount": -0.000001}
            for i in range(start, stop)
        ]

    def test_tracker_keeps_counting_when_the_100_row_window_rotates(self):
        tracker = agent.SessionSpendTracker(self.rows(0, 100))
        with patch.object(agent, "charges", return_value=self.rows(1, 101)):
            spent, count = tracker.refresh()
        self.assertEqual(count, 1)
        self.assertAlmostEqual(spent, 0.000001)

        with patch.object(agent, "charges", return_value=self.rows(2, 102)):
            spent, count = tracker.refresh()
        self.assertEqual(count, 1)
        self.assertAlmostEqual(spent, 0.000002)

    def test_tracker_fails_closed_without_stable_charge_ids(self):
        with self.assertRaisesRegex(RuntimeError, "no id/requestId"):
            agent.SessionSpendTracker([{"type": "CHARGE", "amount": -0.01}])

    def test_refresh_fails_closed_if_ledger_never_advances(self):
        tracker = agent.SessionSpendTracker(self.rows(0, 1))
        with (
            patch.object(agent, "charges", return_value=self.rows(0, 1)),
            patch.object(agent.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "not visible"):
                agent.refresh_spend_after_inference(tracker, attempts=2, delay=0.01)
        sleep.assert_called_once_with(0.01)

    def test_non_finite_budgets_are_rejected(self):
        for value in (math.nan, math.inf, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.validated_budget(value)


class DeskSafetyTests(unittest.TestCase):
    def test_bearer_token_requires_https_except_on_loopback(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            agent.DeskAPI(base="http://example.com", jwt="secret")
        self.assertEqual(
            agent.DeskAPI(base="http://127.0.0.1:3000", jwt="secret").base,
            "http://127.0.0.1:3000",
        )

    def test_http_200_error_body_is_not_a_successful_buy(self):
        api = agent.DeskAPI(base="http://127.0.0.1:3000", jwt="secret")
        with patch.object(
            agent.requests,
            "post",
            return_value=FakeResponse(200, {"ok": False, "error": "insufficient_sol"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "insufficient_sol"):
                api.buy("11111111111111111111111111111111", 0.05)

    def test_execution_wallet_must_match_inference_wallet(self):
        snapshot = {
            "hasWallet": True,
            "address": "11111111111111111111111111111111",
            "stats": {"tradesToday": 0, "maxTradesPerDay": 10},
            "balances": {"sol": 1.0},
        }
        with self.assertRaisesRegex(RuntimeError, "does not match inference wallet"):
            agent._validated_execution_snapshot(snapshot)

    def test_snapshot_caps_use_the_stricter_local_limit(self):
        snapshot = {
            "hasWallet": True,
            "address": str(agent.signer.address),
            "stats": {"tradesToday": 4, "maxTradesPerDay": 999},
            "balances": {"sol": 1.0},
        }
        trades, cap, sol = agent._validated_execution_snapshot(snapshot)
        self.assertEqual((trades, cap, sol), (4, agent.MAX_TRADES_PER_DAY, 1.0))

    def test_signature_without_a_mint_never_reaches_paid_evaluation(self):
        self.assertEqual(agent.hard_filters({"signature": "abc"}, 0), "no mint")


if __name__ == "__main__":
    unittest.main()
