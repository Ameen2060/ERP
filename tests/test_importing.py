"""TB / GL / Financial-report import: multi-format parse, validation, analysis and commit."""

from __future__ import annotations

import io
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app


def D(x) -> Decimal:
    return Decimal(str(x))


def _analyze(client, kind, filename, content: bytes, mime="text/csv"):
    return client.post("/api/import/analyze", data={"kind": kind},
                       files={"file": (filename, content, mime)})


def _echo_rows(analysis):
    return [{"row_no": r["row_no"], "code": r["code"], "name": r["name"],
             "debit": r["debit"], "credit": r["credit"], "date": r["date"],
             "reference": r["reference"], "memo": r["memo"]} for r in analysis["rows"]]


TB_CSV = b"""Account Code,Account Name,Debit,Credit
1010,Cash,100000,0
1100,Accounts Receivable,25000,0
3010,Capital,0,125000
"""


def test_tb_csv_analyze_and_commit():
    with TestClient(app) as client:
        r = _analyze(client, "tb", "tb.csv", TB_CSV)
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["balanced"] is True
        assert D(a["total_debit"]) == Decimal("125000.00") == D(a["total_credit"])
        assert a["matched_count"] == 3 and a["unmatched_count"] == 0
        # Commit posts one balanced opening entry.
        c = client.post("/api/import/commit", json={"kind": "tb", "rows": _echo_rows(a), "opening_date": "2025-01-01"})
        assert c.status_code == 200, c.text
        body = c.json()
        assert body["entries_created"] == 1
        je = client.get(f"/api/journal-entries/{body['journal_entry_ids'][0]}").json()
        by = {ln["account_code"]: ln for ln in je["lines"]}
        assert D(by["1010"]["debit"]) == Decimal("100000.00")
        assert D(by["3010"]["credit"]) == Decimal("125000.00")
        assert je["source"] == "opening"


def test_tb_unbalanced_is_flagged_and_rejected():
    with TestClient(app) as client:
        bad = b"Account Code,Account Name,Debit,Credit\n1010,Cash,100,0\n3010,Capital,0,90\n"
        a = _analyze(client, "tb", "bad.csv", bad).json()
        assert a["balanced"] is False
        c = client.post("/api/import/commit", json={"kind": "tb", "rows": _echo_rows(a)})
        assert c.status_code == 400 and "balance" in c.json()["detail"].lower()


def test_tb_create_missing_account():
    with TestClient(app) as client:
        csv = b"Account Code,Account Name,Debit,Credit\n1099,Petty Cash Fund,300,0\n3010,Capital,0,300\n"
        a = _analyze(client, "tb", "new.csv", csv).json()
        new_row = next(r for r in a["rows"] if r["code"] == "1099")
        assert new_row["status"] == "new"
        c = client.post("/api/import/commit", json={
            "kind": "tb", "rows": _echo_rows(a), "opening_date": "2025-01-01", "create_missing": True})
        assert c.status_code == 200, c.text
        assert "1099" in c.json()["accounts_created"]
        assert any(acc["code"] == "1099" for acc in client.get("/api/accounts").json())


def test_gl_grouping_and_commit():
    with TestClient(app) as client:
        gl = (b"Date,Reference,Account Code,Debit,Credit\n"
              b"2025-03-01,JV-100,1010,500,0\n"
              b"2025-03-01,JV-100,4010,0,500\n"
              b"2025-03-02,JV-101,5020,1200,0\n"
              b"2025-03-02,JV-101,1010,0,1200\n")
        a = _analyze(client, "gl", "gl.csv", gl).json()
        assert len(a["groups"]) == 2
        assert all(g["balanced"] for g in a["groups"])
        c = client.post("/api/import/commit", json={"kind": "gl", "rows": _echo_rows(a)})
        assert c.status_code == 200, c.text
        assert c.json()["entries_created"] == 2


def test_gl_skips_unbalanced_group():
    with TestClient(app) as client:
        gl = (b"Date,Reference,Account Code,Debit,Credit\n"
              b"2025-04-01,JV-200,1010,500,0\n"
              b"2025-04-01,JV-200,4010,0,400\n")  # unbalanced
        a = _analyze(client, "gl", "gl2.csv", gl).json()
        c = client.post("/api/import/commit", json={"kind": "gl", "rows": _echo_rows(a), "skip_unbalanced": True})
        assert c.status_code == 200
        assert c.json()["entries_created"] == 0 and len(c.json()["skipped"]) == 1


def test_report_analyze_by_type():
    with TestClient(app) as client:
        rep = (b"Account Code,Account Name,Debit,Credit\n"
               b"4010,Sales Revenue,0,50000\n"
               b"5020,Rent,12000,0\n"
               b"Total,,12000,50000\n")   # subtotal line ignored
        a = _analyze(client, "report", "pl.csv", rep).json()
        types = {t["type"] for t in a["by_type"]}
        assert "income" in types and "expense" in types
        assert not any(r["name"] == "Total" for r in a["rows"])  # subtotal excluded


def test_xlsx_parsing():
    from openpyxl import Workbook
    with TestClient(app) as client:
        wb = Workbook()
        ws = wb.active
        ws.append(["Account Code", "Account Name", "Debit", "Credit"])
        ws.append(["1010", "Cash", 7000, 0])
        ws.append(["3010", "Capital", 0, 7000])
        buf = io.BytesIO()
        wb.save(buf)
        a = _analyze(client, "tb", "tb.xlsx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").json()
        assert a["balanced"] is True and a["matched_count"] == 2
