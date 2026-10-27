from datetime import date

from scripts.backfill_order_flow import build_archive_requests


def test_monthly_order_flow_backfill_uses_daily_archives_for_unclosed_tail_month():
    requests = build_archive_requests(
        date(2026, 6, 15),
        date(2026, 8, 26),
        "monthly",
        current_date=date(2026, 8, 27),
    )

    assert requests[:2] == [
        ("monthly", date(2026, 6, 1)),
        ("monthly", date(2026, 7, 1)),
    ]
    assert requests[2] == ("daily", date(2026, 8, 1))
    assert requests[-1] == ("daily", date(2026, 8, 26))
    assert len(requests) == 28


def test_daily_order_flow_backfill_keeps_each_requested_day():
    assert build_archive_requests(
        date(2026, 8, 24),
        date(2026, 8, 26),
        "daily",
        current_date=date(2026, 8, 27),
    ) == [
        ("daily", date(2026, 8, 24)),
        ("daily", date(2026, 8, 25)),
        ("daily", date(2026, 8, 26)),
    ]
