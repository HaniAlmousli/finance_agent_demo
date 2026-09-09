"""
Unit tests for tools_core.get_transactions — the row-level-secured tool body.

The body is pure stdlib (no AWS, no langchain), so the interesting logic —
user isolation, date-range filtering, the newest-first cap, error paths —
is fully testable locally against a fixture CSV (via the csv_path test seam).

Run:  uv run --with pytest python -m pytest tests/ -q   (from infra/)
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "agent"))

import tools_core  # noqa: E402

SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"
TODAY = date(2026, 9, 1)   # fixture dates are fixed — tests are deterministic


def make_csv(tmp_path, rows_a=12, rows_b=3):
    """Fixture: rows_a transactions for A (one per day, newest = TODAY),
    rows_b for B."""
    lines = ["user_id,date,description,amount,currency,category"]
    for i in range(rows_a):
        d = (TODAY - timedelta(days=i)).isoformat()
        lines.append(f"{SUB_A},{d},A-merchant-{i},-{10 + i}.00,USD,cat-a")
    for i in range(rows_b):
        d = (TODAY - timedelta(days=i * 10)).isoformat()
        lines.append(f"{SUB_B},{d},B-merchant-{i},-{20 + i}.00,USD,cat-b")
    p = tmp_path / "transactions.csv"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_row_level_isolation(tmp_path):
    p = make_csv(tmp_path)
    out_a = tools_core.get_transactions(SUB_A, csv_path=p)
    out_b = tools_core.get_transactions(SUB_B, csv_path=p)
    assert "A-merchant" in out_a and "B-merchant" not in out_a
    assert "B-merchant" in out_b and "A-merchant" not in out_b


def test_no_dates_returns_newest_first_capped(tmp_path):
    p = make_csv(tmp_path, rows_a=12)
    out = tools_core.get_transactions(SUB_A, csv_path=p)
    assert f"{tools_core.MAX_TRANSACTIONS} transaction(s)" in out
    # newest row (TODAY) present; oldest (day 11) cut by the cap
    assert "A-merchant-0" in out
    assert "A-merchant-11" not in out
    # newest first: day 0 appears before day 1
    assert out.index("A-merchant-0") < out.index("A-merchant-1")


def test_date_range_filters_inclusively(tmp_path):
    p = make_csv(tmp_path, rows_a=12)
    start = (TODAY - timedelta(days=3)).isoformat()
    end = (TODAY - timedelta(days=1)).isoformat()
    out = tools_core.get_transactions(SUB_A, start, end, csv_path=p)
    assert "3 transaction(s)" in out                     # days 1, 2, 3
    assert "A-merchant-0" not in out                     # TODAY excluded by end
    assert "A-merchant-1" in out and "A-merchant-3" in out
    assert "A-merchant-4" not in out                     # before start


def test_open_ended_range_is_not_capped(tmp_path):
    p = make_csv(tmp_path, rows_a=12)
    start = (TODAY - timedelta(days=60)).isoformat()
    out = tools_core.get_transactions(SUB_A, start_date=start, csv_path=p)
    assert "12 transaction(s)" in out   # explicit range -> everything in it


def test_unknown_user_gets_empty_not_others_data(tmp_path):
    p = make_csv(tmp_path)
    out = tools_core.get_transactions("nobody-sub", csv_path=p)
    assert "No transactions found" in out
    assert "merchant" not in out


def test_bad_date_is_clean_error(tmp_path):
    p = make_csv(tmp_path)
    out = tools_core.get_transactions(SUB_A, "last tuesday", csv_path=p)
    assert "Error" in out and "YYYY-MM-DD" in out
    assert "merchant" not in out   # tool results never raise, never leak


def test_missing_csv_is_clean_message(tmp_path):
    out = tools_core.get_transactions(SUB_A, csv_path=str(tmp_path / "nope.csv"))
    assert "not available" in out


def test_provenance_tag_present(tmp_path):
    p = make_csv(tmp_path)
    assert tools_core.get_transactions(SUB_A, csv_path=p) \
        .startswith("[TOOL: get_transactions]")
    assert tools_core.get_transactions(SUB_A, csv_path=p, where=" @lambda") \
        .startswith("[TOOL: get_transactions @lambda]")
