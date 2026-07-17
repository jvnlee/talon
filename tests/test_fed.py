from datetime import date

import pytest

from talon.errors import SourceError
from talon.sources.fed import parse_fomc_calendar, parse_fomc_historical

CALENDAR_HTML = """
<div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div>
<div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">30-1</div>
<div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>June</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">16-17*</div>
<div class="panel-heading"><h4><a id="42829">2027 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">26-27</div>
"""

HISTORICAL_HTML = """
<h5 class="panel-heading panel-heading--shaded">January 26-27 Meeting - 2016</h5>
<h5 class="panel-heading panel-heading--shaded">November 1-2 Meeting - 2016</h5>
<h5 class="panel-heading panel-heading--shaded">April 30-May 1 Meeting - 2019</h5>
<h5 class="panel-heading panel-heading--shaded">March 15 (unscheduled) Meeting - 2020</h5>
"""


def test_parse_fomc_calendar_extracts_decision_days():
    days = parse_fomc_calendar(CALENDAR_HTML)

    assert date(2026, 1, 28) in days
    assert date(2026, 6, 17) in days
    assert date(2027, 1, 27) in days


def test_parse_fomc_calendar_handles_cross_month_meetings():
    days = parse_fomc_calendar(CALENDAR_HTML)

    assert date(2026, 5, 1) in days


def test_parse_fomc_calendar_rejects_markup_drift():
    with pytest.raises(SourceError, match="연도 패널"):
        parse_fomc_calendar("<html><body>nothing here</body></html>")


def test_parse_fomc_calendar_rejects_empty_meetings():
    html = '<div><h4><a id="1">2026 FOMC Meetings</a></h4></div>'

    with pytest.raises(SourceError, match="한 건도"):
        parse_fomc_calendar(html)


def test_parse_fomc_historical():
    days = parse_fomc_historical(HISTORICAL_HTML)

    assert days == {
        date(2016, 1, 27),
        date(2016, 11, 2),
        date(2019, 5, 1),
        date(2020, 3, 15),
    }
