# Go-live checklist (production)

Source: Loc's message of 21 Nov 2026 ("Before going live"), HANDOFF-07 task E5. Loc's own end-to-end test ran on a local database with NEAC, OCR, Zalo, TPV and the scheduler's login faked or bypassed. **This list is what proves each of those works in production with real accounts.**

How to use it: tick a box only when you have seen the expected result with your own eyes, and write the date and your initials. A box left empty is a known gap, not a pass.

```bash
PROJECT=fundlok; REGION=asia-southeast1
SA=cloud-scheduler@fundlok.iam.gserviceaccount.com
URL=$(gcloud run services describe prod-api --region $REGION --project $PROJECT --format='value(status.url)')
```

Owner: Edward unless a row says Phat. Who sets a value is the person with access to the secret.

---

## 1. Settings on the `prod-api` Cloud Run service

Read them all at once, then compare:

```bash
gcloud run services describe prod-api --region $REGION --project $PROJECT --format=yaml
```

Secret values (keys, passwords) may show as `valueFrom: secretKeyRef`. That is fine: check the reference exists, do not print the secret.

| # | Setting | Expected | Why | OK |
|---|---|---|---|---|
| 1.1 | `FUNDING_WINDOW_FLOW` | `COMMIT_THEN_ACCEPT` | The default is `LEGACY`: new rounds would use the old pay-at-commit flow | [x] |
| 1.2 | `FUNDLOK_FEE_BANK_BIN` | `970407` (Techcombank) | SME fee QR and the fee email's bank block; unset = no QR | [x] |
| 1.3 | `FUNDLOK_FEE_ACCOUNT_NUMBER` | `116889688` | Same | [x] |
| 1.4 | `FUNDLOK_FEE_ACCOUNT_NAME` | the account holder's name as the bank has it | Same | [x] |
| 1.5 | `SCHEDULER_SERVICE_ACCOUNT` | `cloud-scheduler@fundlok.iam.gserviceaccount.com` | Unset: every scheduled job answers 503 | [x] |
| 1.6 | `SCHEDULER_AUDIENCE` | exactly the `prod-api` URL (no path, no trailing slash) | A mismatch: every job answers 401 | [x] |
| 1.7 | `ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID` | the approved template; parameters `company_name`, `amount`, `deadline` (Phat checks the OA) | Unset: SME acceptance reminders go in-app and by email only | [ ] |
| 1.8 | `ZALO_ZBS_DAY14_TEMPLATE_ID` | the approved template; parameters `company_name`, `amount`, `deadline` | Unset: Day-14 reminders go in-app and by email only | [ ] |
| 1.9 | `ZALO_ZBS_REMINDER_TEMPLATE_ID` | `666161` (`fundlok_reminder`) | Repayment reminder on Zalo | [x] |
| 1.10 | `ZALO_ZBS_OTP_TEMPLATE_ID`, `ZALO_APP_ID`, `ZALO_APP_SECRET`, `ZALO_OA_REFRESH_TOKEN`, `ZALO_TOKEN_ENCRYPTION_KEY` | all set | Zalo codes (sign-up, guarantor, lender signing) cannot be sent without them | [x] |
| 1.11 | `ZALO_ZBS_DEVELOPMENT_MODE` | unset or `false` | In development mode Zalo delivers only to the OA's admins | [x] |
| 1.12 | `PHONE_OTP_CHANNEL` | `zalo` (not `log`) | `log` writes codes to the server log | [x] |
| 1.13 | `RECEIPT_OCR_ENABLED` | `1` | Default is off: receipts are not read at all. Loc (22 Nov): OCR is the first barrier, "a lock on the door", not the only control | [x] |
| 1.14 | `RECEIPT_AUTO_VERDICT_ENABLED` | `1` (Loc, 22 Nov: OCR works; the real control is confirming payments against bank statements) | Default is off: a confident read would not accept or deny a receipt | [x] |
| 1.15 | `GVERIFY_BASE_URL`, `GVERIFY_API_KEY`, `GVERIFY_PARTNER_CODE` | all set (TPV Datatrust, live) | Without them KYC and KYB cannot run | [x] |
| 1.16 | `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | set, bucket exists | Receipts, contracts and signed documents are stored there | [ ] |
| 1.17 | `NEAC_VERIFY_URL` | unset (default `https://neac.gov.vn/vi/Home/VerifyFileByNeac`) or the value NEAC gave | The signature check on signed PDFs | [x] |
| 1.18 | `FRONTEND_URL` | the production web address | Links in emails and Zalo | [x] |
| 1.19 | `DEBUG` | unset or `false` | Never on in production | [x] |
| 1.20 | `GVERIFY_KYB_REQUIRE_REP_MATCH` | `true` | Default is off. Loc (22 Nov): no separate guarantor; the owner or legal representative signs up, does KYB and KYC, signs and is the guarantor. On, KYB is refused unless the KYC person is a legal representative on the certificate (`NOT_A_REPRESENTATIVE`) | [x] |

---

## 2. Cloud Scheduler jobs

```bash
gcloud scheduler jobs list --location $REGION --project $PROJECT
```

Every job: region `asia-southeast1`, time zone `Asia/Ho_Chi_Minh`, HTTP POST to `$URL/internal/jobs/<route>`, OIDC token for `$SA`, audience `$URL`, attempt deadline 300s, 3 retries. Commands are in `docs/specs/scheduled_jobs/cloud-scheduler-jobs.md` section 9.

| # | Job | Cron (ICT) | Route | Body | Created | Fired once (200) |
|---|---|---|---|---|---|---|
| 2.1 | `repayment-reminders` | `0 15 * * *` | `/internal/jobs/repayment-reminders` | none | [x] | [x] |
| 2.2 | `fee-invoice-emails` | `5 15 * * *` | `/internal/jobs/fee-invoice-emails` | none | [x] | [x] |
| 2.3 | `funding-advance` | `0 0,15 * * *` | `/internal/jobs/funding-advance` | none | [x] | [ ] |
| 2.4 | `funding-advance-day14` | `40 15 * * *` | `/internal/jobs/funding-advance` | none | [x] | [ ] |
| 2.5 | `acceptance-reminders` | `0 9 * * *` | `/internal/jobs/acceptance-reminders` | none | [x] | [ ] |
| 2.6 | `day14-reminders-morning` | `0 8 * * *` | `/internal/jobs/day14-transfer-reminders` | `{"kind": "DAY14_MORNING"}` | [x] | [ ] |
| 2.7 | `day14-reminders-afternoon` | `0 14 * * *` | `/internal/jobs/day14-transfer-reminders` | `{"kind": "DAY14_AFTERNOON"}` | [x] | [ ] |
| 2.8 | `day14-cutoff` | `0,35 15 * * *` | `/internal/jobs/day14-cutoff` | none | [x] | [ ] |
| 2.9 | `repayment-day` | `10 0,18 * * *` | `/internal/jobs/repayment-day` | none | [x] | [x] |
| 2.10 | `invoices` | `20 0 * * *` | `/internal/jobs/invoices` | none | [x] | [x] |
| 2.11 | `cic-daily-balance` | `30 0 * * *` | `/internal/jobs/cic-daily-balance` | none | [x] | [x] |

Fire each once and read the result:

```bash
gcloud scheduler jobs run <job> --location $REGION --project $PROJECT
gcloud logging read 'resource.type="cloud_run_revision" AND httpRequest.requestUrl:"/internal/jobs/"' \
  --project $PROJECT --limit 12 --format='table(timestamp,httpRequest.requestUrl,httpRequest.status)'
```

`200` is a pass. `401`: wrong audience or service account. `403`: a token for another account. `503`: the two `SCHEDULER_*` settings are missing, or maintenance mode is on. `404`: the new revision is not deployed yet.

Careful with 2.9: its first run marks any unpaid due repayment day Late and settles paid days into the ledger. Check that production holds no leftover test repayment days first.

"Fire once" proves the call works. The real proof is the next scheduled time: after the first 00:10 or 18:10, confirm a log line for it.

| # | Check | OK |
|---|---|---|
| 2.12 | Each job's last run at its scheduled time (not only the manual run) shows `200` the next day | [ ] |

### Evidence so far (Cloud Run logs, `gcloud logging read`; times UTC)

- **2.1, 2.2** (scheduled runs, 23 Nov): `repayment-reminders` 08:00 and `fee-invoice-emails` 08:05, both `200`.
- **2.10, 2.11** (fired by hand, 23 Nov 20:40): `invoices` and `cic-daily-balance`, both `200` through the real OIDC token. This also proves 1.5, 1.6 and 3.7 (an unset setting would be 503, a wrong audience 401, a wrong account 403).
- **2.9** `repayment-day`: the first run (23 Nov 20:46) returned **500**: `asyncpg InterfaceError: connection is closed` in the maintenance-mode middleware, not in the job. Cause: the app's database pool had no health check and Neon closes idle connections, so the first request after about five idle minutes failed for any endpoint. Fixed in #155 (`pool_pre_ping`, `pool_recycle=240`, regression test). After the deploy, two runs **7 min 10 s apart** (24 Nov 01:32:41 and 01:39:51) both returned `200`.
- All 11 jobs exist, are `ENABLED`, in `Asia/Ho_Chi_Minh`, with the right route, audience and service account; the two Day-14 reminder jobs carry `{"kind": "DAY14_MORNING"}` and `{"kind": "DAY14_AFTERNOON"}`.
- **Settings (24 Nov, `gcloud run services describe prod-api`, secret values not printed):** `FUNDLOK_FEE_BANK_BIN` 970407, `FUNDLOK_FEE_ACCOUNT_NUMBER` 116889688 and the account name are set (1.2 to 1.4); `ZALO_ZBS_REMINDER_TEMPLATE_ID` 666161 (1.9); all secret-type Zalo and TPV settings are set (1.10, 1.15); `ZALO_ZBS_DEVELOPMENT_MODE` false (1.11); `PHONE_OTP_CHANNEL` zalo (1.12); `NEAC_VERIFY_URL` unset, so the default (1.17); `FRONTEND_URL` https://www.fundlok.com (1.18); `DEBUG` unset (1.19); `GVERIFY_KYB_REQUIRE_REP_MATCH` true (1.20). "Set" proves the setting exists, not that the service works: that is section 3.
- **Fixed that day:** `FUNDING_WINDOW_FLOW`, `RECEIPT_OCR_ENABLED` and `RECEIPT_AUTO_VERDICT_ENABLED` were UNSET (the flow would have defaulted to `LEGACY`). One `gcloud run services update --update-env-vars` created revision `prod-api-00297-ntb`; re-read shows `COMMIT_THEN_ACCEPT`, `1`, `1` (1.1, 1.13, 1.14), and `invoices` returned `200` on it.
- **Still open in section 1:** 1.7 and 1.8 (the two Zalo template IDs, from Phat or Loc) and 1.16 (R2 settings are set; the bucket is proven by the receipt upload in 3.6).
- Still to see: 2.3 to 2.8 firing on their own schedule (08:00, 09:00, 14:00, 15:00 and 15:35 Vietnam time), and 2.12 (each job `200` at its scheduled time the next day, including `repayment-day` at 00:10 and 18:10 after a long idle gap).

---

## 3. Real accounts, not mocks (Loc asked for these four)

Do each with a real person, real documents and the real service. Write what you used and what happened.

| # | What | How to prove it | Owner | OK |
|---|---|---|---|---|
| 3.1 | **TPV KYB** (company check against the tax registry) | Submit a real company's tax code and documents through the live KYB flow. Expect `APPROVED`; also try one wrong tax code and confirm it is rejected (thresholds: `GVERIFY_MIN_KYB_OCR_CONFIDENCE`) | Phat | [ ] |
| 3.2 | **TPV KYC** (eKYC of a person) | One real ID card and selfie through the live flow. Expect `APPROVED` and a name that matches the card | Phat | [ ] |
| 3.3 | **CA token and the NEAC check** | A real SME signs the Standing Offer PDF with its USB/CA token and uploads it. The admin panel's signature check shows NEAC's verdict as valid and every check passing. Also upload a copy altered after signing and confirm the check fails | Edward | [ ] |
| 3.4 | **Zalo codes** | A real phone receives the sign-up code, then the guarantor code, then a lender signing code. Each arrives in under a minute from the production OA | Phat | [ ] |
| 3.5 | **Zalo reminder templates** | Trigger the acceptance reminder and the Day-14 reminder for a test account on a real phone; parameters (`company_name`, `amount`, `deadline`) are filled in, not blank | Phat | [ ] |
| 3.6 | **Receipt OCR** | Upload three real bank receipts in production: one correct, one with a wrong amount, one blurry. Expect: matched, denied, and sent to staff | Edward | [ ] |
| 3.7 | **The scheduler login** | Job rows 2.9 to 2.11 returned `200` through the real OIDC token (not a bypass) | Edward | [x] |
| 3.8 | **Fee QR** | An SME fee invoice shows a QR; scan it with a bank app and confirm the account is Techcombank 116889688 with the right amount and reference | Edward | [ ] |
| 3.9 | **Late status and investor invoices end to end** | After 2.9 and 2.10 have run on real data: an unpaid due day shows Late, a paid day settles after 24 hours, an investor invoice exists and is readable by the lender | Edward | [ ] |

---

## 4. Known gaps at go-live (tell Loc; these are decisions, not failures)

| # | Gap | State | Where recorded |
|---|---|---|---|
| 4.1 | Uploaded files are not malware-scanned | Off since #73; not on Loc's launch list | `docs/outstanding-issues.md` TD-05 |
| 4.2 | Top-up signing is not built | Launch rule: staff approve late money before 15:30 (`late-money-approval.md`) | `late-money-approval.md` |
| 4.3 | Late money, and what happens after 15:30 | Loc (22 Nov): top-up signing is the intended way and not a launch blocker. At launch staff approve late money into the loan (`late-money-approval.md`) and manage anything later by hand, even after 15:30, outside the platform; the SME does not refund | `docs/outstanding-issues.md` TD-06, D-061 |
| 4.4 | Per-lender schedules can differ from a lender's own contract by 1 to 30 đồng | Loc agreed the rule (22 Nov); Phat implements `per-lender-rounding.md` (E2) | `per-lender-rounding.md` |
| 4.5 | `expire_holds_job` and `advance_windows_job` are still ARQ-only (no worker) | Not needed by the commit-then-accept flow; the dispute-deadline step of `advance_windows_job` is not run by any route | `cloud-scheduler-jobs.md` section 10 |
| 4.6 | Public holidays are not skipped (the schedule bills them) | Decided (TD-01); the contract text must say so (Phat, contracts v1.0) | `docs/outstanding-issues.md` TD-01 |

---

## 5. Sign-off

| Who | Date | Notes |
|---|---|---|
| Edward | | |
| Phat | | |
| Loc (last manual test) | | |
