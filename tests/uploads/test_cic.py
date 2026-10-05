"""Step 4: the CIC credit report — score, rank and debt position.

The fixture is SYNTHETIC layout-mode text shaped like the reference report
(an individual's report, score 629, rank 2, 17 million of card debt). The real
report is not committed: it carries a person's ID number, birth date and
address. Checked against it during development: every field below matched.
"""
import io
from datetime import date

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from app.uploads.models import CicSummary
from app.uploads.parsers.cic import CicParseError, parse_cic_pdf, parse_cic_text
from conftest import auth_headers

BANDS = "403-429   430-454    455-479  480-544    545-571   572-587   588-605   606-621  622-644    645-706"


def report_text(*, kind="CÁ NHÂN", score="629", rank="2", scored="23/04/2026", queried="07/05/2026",
                percentile="85", card_standard="17", attention="", bad="", history=True, bands=BANDS) -> str:
    history_line = (
        "Khách hàng không có lịch sử nợ xấu, nợ cần chú ý tại các TCTD"
        if history
        else "Lịch sử nợ cần chú ý: 1 khoản"
    )
    total = sum(float(x) for x in (card_standard, attention, bad) if x)
    return f"""
                                         TRUNG TÂM THÔNG TIN TÍN DỤNG QUỐC GIA VIỆT NAM
                                         Email: htkh@creditinfo.org.vn   Web: https://cic.gov.vn
Số: 2026/K11.1
                THÔNG TIN TÍN DỤNG KHÁCH HÀNG VAY {kind}
Ngày hỏi tin: {queried} 16:46:12                                                 Ngày trả lời: {queried} 16:46:12
1.  Thông tin định danh
  Tên khách hàng:          NGUYỄN VĂN MẪU
  Số CCCD/CMND:            000000000000
2.1. Thông tin dư nợ hiện thời
                                                                                         Ðơn vị tính: triệu đồng, USD
                    Tên tổ chức/chi nhánh TCTD                                      Nợ vay                  Nợ thẻ
                              Loại dư nợ                                     VND              USD            VND
 1. Ngân hàng TMCP Mẫu - Chi nhánh Một
 Tổng nợ                                                                              0               0             {total:g}
  - Nợ đủ tiêu chuẩn                                                                                                {card_standard}
  - Nợ cần chú ý                                                                                                    {attention}
  - Nợ xấu                                                                                                          {bad}
                              Tổng cộng                                               0               0             {total:g}
Lưu ý:
  - Nợ xấu gồm các khoản nợ được phân loại nhóm 3, 4, 5 theo các quy định phân loại nợ có liên quan của
2.2. Thông tin lịch sử nợ cần chú ý và nợ xấu
{history_line}
2.3. Thông tin về đảm bảo tiền vay
    1       Bảo đảm tiền vay bằng tài sản                                Không có
II. THÔNG TIN ĐIỂM TÍN DỤNG
       Điểm tín dụng:                Xếp hạng của                     100%
             {score}                      khách hàng               {percentile}             Điểm tín dụng của khách hàng cao
            Hạng:                                                             hơn  điểm  tín  dụng  của  {percentile}%  tổng
                                                                      50%     khách hàng cá nhân được chấm điểm
               {rank}                                                              tại kho dữ liệu CIC.
      Ngày chấm điểm:
         {scored}                                                   0%
      Điểm    {bands}
"""


FRESH = date(2026, 5, 10)


def codes(report):
    return [w["code"] for w in report.warnings]


def test_reads_the_score_rank_and_date():
    r = parse_cic_text(report_text(), signed=False, today=FRESH)
    assert (r.score, r.rank, r.scored_on) == (629, 2, "2026-04-23")
    assert r.rank_band == [622, 644] and r.rank_label == "very_good"
    assert r.percentile == 85
    assert r.subject_type == "individual" and r.subject_name == "NGUYỄN VĂN MẪU"


def test_reads_the_value_under_its_label_not_the_chart_beside_it():
    # "Điểm tín dụng:" shares its line with the chart's "100%", and "Hạng:"
    # with the percentile sentence; neither may be read as the value.
    r = parse_cic_text(report_text(score="480", rank="7", percentile="12"), signed=False, today=FRESH)
    assert (r.score, r.rank) == (480, 7)


def test_reads_the_debt_position_in_million_dong():
    r = parse_cic_text(report_text(), signed=False, today=FRESH)
    assert r.debt_total_vnd_million == 17 and r.debt_total_usd == 0
    assert r.debt_standard_vnd_million == 17
    assert r.debt_attention_vnd_million == 0 and r.debt_bad_vnd_million == 0
    assert r.negative_history is False and r.lenders == 1
    assert r.collateral == "Không có"


def test_current_bad_debt_is_flagged():
    r = parse_cic_text(report_text(card_standard="10", bad="5"), signed=False, today=FRESH)
    assert r.debt_bad_vnd_million == 5 and "BAD_DEBT" in codes(r)


def test_negative_history_is_flagged():
    r = parse_cic_text(report_text(history=False), signed=False, today=FRESH)
    assert r.negative_history is True and "NEGATIVE_HISTORY" in codes(r)


def test_a_score_outside_its_rank_band_is_flagged():
    r = parse_cic_text(report_text(score="629", rank="5"), signed=False, today=FRESH)
    assert "SCORE_RANK_MISMATCH" in codes(r)


def test_an_individual_unsigned_report_says_so():
    assert {"INDIVIDUAL_REPORT", "UNSIGNED"} <= set(codes(parse_cic_text(report_text(), signed=False, today=FRESH)))


def test_a_business_report_is_recognised():
    r = parse_cic_text(report_text(kind="DOANH NGHIỆP"), signed=False, today=FRESH)
    assert r.subject_type == "business" and "INDIVIDUAL_REPORT" not in codes(r)


def test_an_old_report_is_flagged_as_stale():
    r = parse_cic_text(report_text(), signed=False, today=date(2026, 9, 27))
    assert "STALE_REPORT" in codes(r)
    assert "STALE_REPORT" not in codes(parse_cic_text(report_text(), signed=False, today=FRESH))


def test_a_report_without_a_score_is_kept_with_a_warning():
    r = parse_cic_text(report_text(score="—", rank="—"), signed=False, today=FRESH)
    assert r.score is None and r.rank is None and "NO_SCORE" in codes(r)


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize("raw,code", [
    (b"not a pdf", "NOT_A_PDF"),
    (b"%PDF-1.4 broken", "UNREADABLE_PDF"),
])
def test_pdf_refusals(raw, code):
    with pytest.raises(CicParseError) as exc:
        parse_cic_pdf(raw)
    assert exc.value.code == code


def test_a_scan_with_no_text_is_refused():
    with pytest.raises(CicParseError) as exc:
        parse_cic_pdf(blank_pdf())
    assert exc.value.code == "UNREADABLE_PDF"


def test_a_pdf_that_is_not_a_cic_report_is_refused():
    with pytest.raises(CicParseError) as exc:
        parse_cic_text("HÓA ĐƠN GIÁ TRỊ GIA TĂNG", signed=False)
    assert exc.value.code == "NOT_CIC"


# --- endpoints -------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch):
    # Any PDF bytes; the text layer is the synthetic report.
    monkeypatch.setattr("app.uploads.parsers.cic._text", lambda raw: report_text())
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct, **kw: f"https://r2.test/{key}")
    monkeypatch.setattr(
        "app.uploads.service.head_object", lambda key, **kw: {"size": 1024, "content_type": "application/pdf"}
    )
    monkeypatch.setattr("app.uploads.service.get_bytes", lambda key, **kw: blank_pdf())
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: None)


@pytest.fixture
async def draft(make_user, make_project, make_loan_application):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"])
    return sme, application["id"]


async def test_preview_shows_step_4_what_was_read(client, draft, storage):
    sme, app_id = draft
    resp = await client.post(
        f"/uploads/cic-preview?loan_application_id={app_id}",
        content=blank_pdf(),
        headers={**auth_headers(sme), "Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["score"], body["rank"], body["rank_label"]) == (629, 2, "very_good")
    assert body["debt_total_vnd_million"] == 17 and body["negative_history"] is False
    assert body["subject_type"] == "individual" and body["age_days"] is not None


async def test_preview_refusal_carries_a_code(client, draft):
    sme, app_id = draft
    resp = await client.post(
        f"/uploads/cic-preview?loan_application_id={app_id}",
        content=b"not a pdf",
        headers={**auth_headers(sme), "Content-Type": "application/pdf"},
    )
    assert resp.status_code == 422
    assert resp.headers["x-error-code"] == "CIC_NOT_A_PDF"


async def test_confirm_stores_the_score_and_grading_inputs_use_it(client, db_session, make_admin, draft, storage):
    sme, app_id = draft
    init = await client.post(
        "/uploads/init-upload",
        json={"loan_application_id": app_id, "document_type": "cic_report", "filename": "cic.pdf",
              "content_type": "application/pdf", "size": 1024},
        headers=auth_headers(sme),
    )
    assert init.status_code == 201, init.text
    confirm = await client.post(
        "/uploads/confirm", json={"loan_application_id": app_id, "file_keys": [init.json()["file_key"]]},
        headers=auth_headers(sme),
    )
    assert confirm.status_code == 200, confirm.text
    summary = (await db_session.execute(select(CicSummary))).scalar_one()
    assert (summary.score, summary.rank, summary.scored_on) == (629, 2, date(2026, 4, 23))
    # Personal identifiers are not kept.
    assert "000000000000" not in str(summary.report)

    admin = await make_admin()
    body = (await client.get(f"/admin/applications/{app_id}/grading-inputs", headers=auth_headers(admin))).json()
    assert body["fields"]["cic_score"] == {
        "value": 629,
        "source": "cic",
        "status": "filled",
        "note": body["fields"]["cic_score"]["note"],
    }
    assert "owner" in body["fields"]["cic_score"]["note"]
    assert "cic_score" not in body["blockers"]
    assert body["cic"]["debt_total_vnd_million"] == 17
