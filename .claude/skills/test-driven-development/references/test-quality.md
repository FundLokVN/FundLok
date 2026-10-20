# Writing good tests (general guidance)

Generic practice behind the FundLok-specific rules in `SKILL.md`.

## Contents
- Test state, not interactions
- DAMP over DRY
- Arrange-Act-Assert, one concept per test
- Test sizes / pyramid
- Anti-patterns

## Test state, not interactions
Assert on outcomes (response body, rows in the DB, ledger balances), not on
which internal functions were called. Interaction assertions break on every
refactor and pass when the observable behaviour is wrong.

```python
# Good: observable outcome
resp = await client.post(f"/market/orders", json=body, headers={**h, "Idempotency-Key": key})
again = await client.post(f"/market/orders", json=body, headers={**h, "Idempotency-Key": key})
assert again.json()["id"] == resp.json()["id"]
assert (await db_session.scalar(select(func.count()).select_from(Order))) == 1

# Weak: asserts implementation detail
mock_service.create_order.assert_called_once()
```

## DAMP over DRY
Each test should read top to bottom without jumping into helpers. Shared
*setup* belongs in `conftest.py` factories. The values that matter to the
assertion (amount, role, state) stay visible in the test body.

## Arrange-Act-Assert, one concept per test
```python
async def test_order_rejected_for_investor_without_kyc(client, make_user, open_listing):
    # Arrange
    investor = await make_user(role="INVESTOR")          # no KYC approval
    listing = await open_listing()
    # Act
    resp = await client.post("/market/orders", json={...}, headers=investor["headers"])
    # Assert
    assert resp.status_code == 403
    assert "detail" in resp.json()
```
Several asserts are fine if they describe one behaviour. Split the test when
they describe two.

## Test sizes
| Size | Touches | Use for |
|---|---|---|
| Small | pure functions, no DB | rate/amortization math, schedule generation, validators |
| Medium | ASGI app + throwaway Postgres (the default here) | endpoints, services, ledger postings, migrations |
| Large | multiple services / real partners | not in this suite (partners are mocked) |

Push calculation logic into pure functions so most of the math can be tested
small and fast, with a few medium tests proving the wiring.

## Anti-patterns
| Anti-pattern | Why it hurts | Instead |
|---|---|---|
| Testing implementation details | breaks on refactor, misses real bugs | assert on state/outputs |
| Mocking the DB session or the unit under test | constraints and triggers never fire | real throwaway Postgres; mock only outbound clients |
| Shared mutable state between tests | order-dependent flakiness | factories per test; truncation already isolates the DB |
| `time.sleep` / wall-clock dependence | flaky, slow | inject or freeze the clock |
| Asserting only status codes on financial writes | duplicate rows slip through | also count rows / check balances |
| Skipping a failing test to get green | hides the regression | fix it, or mark xfail with an issue link and tell the human |
| Writing tests after the code "to get coverage" | tests mirror the code's bugs | write the failing test first |
