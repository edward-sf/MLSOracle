import numpy as np
import pandas as pd
import pytest
from data.sources.mlspa import client as mlspa_client
from data.sources.mlspa.client import (
    _CLUB_ALIAS_LOOKUP,
    _CLUB_ALIASES,
    _HEADER_ALIAS_LOOKUP,
    _HEADER_ALIASES,
    _INTERMEDIATE_COLUMNS,
    _SALARY_REPORTS,
    Client,
    _get_page_fixup,
    _get_page_merger,
    _get_pipeline,
    _parse_args,
)


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


def _capture_to_csv(monkeypatch):
    """Patch DataFrame.to_csv so fetch() never touches the real reports/ directory."""
    calls = []

    def fake_to_csv(self, path_or_buf=None, **kwargs):
        calls.append({"df": self.copy(), "path": path_or_buf, "kwargs": kwargs})

    monkeypatch.setattr(pd.DataFrame, "to_csv", fake_to_csv)
    return calls


# --- Page mergers ---------------------------------------------------------


def test_strip_repeated_page_headers_drops_title_and_dedupes_header():
    page1 = pd.DataFrame(
        [
            ["", None, None, None, None, 2008, "2008 Guaranteed"],
            ["Club", "Last Name", "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["LA", "Adzemian", "Vardan", "D", "$", "12,900.00", "$12,900.00"],
        ]
    )
    page2 = pd.DataFrame(
        [
            ["", None, None, None, None, 2008, "2008 Guaranteed"],
            ["Club", "Last Name", "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["SJ", "Ayres", "Jay", "D", "$", "17,700.00", "$17,700.00"],
        ]
    )

    result = Client._strip_repeated_page_headers([page1, page2])

    assert len(result) == 3  # 1 header + 2 data rows
    assert result.iloc[0].tolist()[0] == "Club"
    assert result.iloc[1, 0] == "LA"
    assert result.iloc[2, 0] == "SJ"


def test_strip_repeated_page_headers_empty_pages_returns_empty_frame():
    assert Client._strip_repeated_page_headers([]).empty


def test_strip_single_header_row_keeps_only_first_header_occurrence():
    page1 = pd.DataFrame(
        [
            ["Club", "Last Name", "First Name", "Pos", "Base Salary", "Compensation"],
            ["Philadelphia Union", "Aaronson", "Brenden", "M-F", "$70,000.08", "$98,309.48"],
        ]
    )
    page2 = pd.DataFrame(
        [
            ["Club", "Last Name", "First Name", "Pos", "Base Salary", "Compensation"],
            ["Toronto FC", "Benezet", "Nicolas", "M-F", "$600,000.00", "$600,000.00"],
        ]
    )

    result = Client._strip_single_header_row([page1, page2])

    assert len(result) == 3
    assert result.iloc[0, 0] == "Club"
    assert list(result.iloc[1:, 0]) == ["Philadelphia Union", "Toronto FC"]


# --- Year-specific page fixups ---------------------------------------------


def test_fix_2016_last_page_realigns_only_the_8_column_page():
    normal_page = pd.DataFrame(
        [
            ["Club", "Last Name", "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["NYRB", "Abang", "Anatole", "F", "$", "62,500.00", "$62,500.00"],
        ]
    )
    malformed_page = pd.DataFrame(
        [
            [None, None, None, None, None, None, "2016", "2016 Guaranteed"],
            ["Club", "Last Name", None, "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["NYRB", "Zizzo", "Sal", None, "M", "$", "105,000.00", "$105,000.00"],
        ]
    )

    fixed = Client._fix_2016_last_page([normal_page, malformed_page])

    assert fixed[0].shape[1] == 7  # untouched
    assert fixed[1].shape[1] == 7
    assert list(fixed[1].iloc[0]) == ["NYRB", "Zizzo", "Sal", "M", "$", "105,000.00", "$105,000.00"]


def test_fix_2017_merged_club_column_splits_only_merged_pages():
    merged_page = pd.DataFrame(
        [
            ["Club Last Name", None, "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["NYRB Abang", None, "Anatole", "F", "$", "65,625.00", "$65,625.00"],
        ]
    )
    already_split_page = pd.DataFrame(
        [
            ["Club", "Last Name", "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["RSL", "Wingert", "Chris", "D", "$", "134,004.00", "$145,394.00"],
        ]
    )

    fixed = Client._fix_2017_merged_club_column([merged_page, already_split_page])

    assert list(fixed[0].iloc[0, :2]) == ["Club", "Last Name"]
    assert list(fixed[0].iloc[1, :2]) == ["NYRB", "Abang"]
    # already-correct page must be untouched, not clobbered with NaN
    assert list(fixed[1].iloc[1, :2]) == ["RSL", "Wingert"]


def test_fix_2020_salary_column_drops_label_and_relabels_value_column():
    df = pd.DataFrame(
        [
            ["Last Name", "First Name", "Club", "Pos", "Base Salary", None, "Comp"],
            ["Aaronson", "Brenden", "Philadelphia Union", "M", None, "$85,000", "$103,309"],
        ]
    )

    fixed = Client._fix_2020_salary_column(df)

    assert fixed.shape[1] == 6
    assert list(fixed.columns) == list(range(6))
    assert fixed.iloc[0, 4] == "Base Salary"
    assert fixed.iloc[1, 4] == "$85,000"


def test_fix_2022_pages_dedupes_consecutive_pairs_and_drops_blob_row():
    real_page = pd.DataFrame(
        [
            ["garbled full-page blob", None, None, None, None, None],
            ["Club", "Last Name", "First Name", "Position", "Base Salary", "Comp"],
            ["Atlanta United", "Almada", "Thiago", "M-F", "$1,650,000.00", "$2,332,000.00"],
        ]
    )
    duplicate_page = real_page.copy()

    fixed = Client._fix_2022_pages([real_page, duplicate_page])

    assert len(fixed) == 1
    assert len(fixed[0]) == 2  # blob row dropped, header + 1 data row remain
    assert fixed[0].iloc[0, 0] == "Club"


def test_fix_2022_garbled_row_corrects_known_row_only():
    df = pd.DataFrame(
        {
            "Club": ["Vancouver Whi", "Toronto FC"],
            "Last Name": ["eWcahpites", "Benezet"],
            "First Name": ["Brian", "Nicolas"],
        }
    )

    fixed = Client._fix_2022_garbled_row(df)

    assert fixed.loc[0, "Club"] == "Vancouver Whitecaps"
    assert fixed.loc[0, "Last Name"] == "White"
    assert fixed.loc[1, "Club"] == "Toronto FC"
    assert fixed.loc[1, "Last Name"] == "Benezet"


# --- Column-level cleaning steps -------------------------------------------


def test_promote_first_row_as_header():
    df = pd.DataFrame([["Club", "Last Name"], ["LA", "Adzemian"]])

    result = Client._promote_first_row_as_header(df)

    assert list(result.columns) == ["Club", "Last Name"]
    assert result.columns.name is None
    assert len(result) == 1
    assert result.iloc[0].tolist() == ["LA", "Adzemian"]


def test_normalize_null_placeholders_converts_stringified_none():
    df = pd.DataFrame({"Club": ["LA", "None"], "Pos": ["D", "M"]})

    result = Client._normalize_null_placeholders(df)

    assert result.loc[0, "Club"] == "LA"
    assert pd.isna(result.loc[1, "Club"])
    assert result.loc[1, "Pos"] == "M"  # untouched, no "None" here


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 2,900.00", "12,900.00"),
        ("1 ,500,000.00", "1,500,000.00"),
        ("5 ,500,000.08", "5,500,000.08"),
        ("160,000.00", "160,000.00"),  # already clean, no-op
        ("$", "$"),  # not currency-shaped, untouched
        ("D", "D"),  # non-string-adjacent value, untouched
    ],
)
def test_strip_currency_whitespace(raw, expected):
    assert Client._strip_currency_whitespace(raw) == expected


def test_strip_currency_whitespace_ignores_non_strings():
    assert Client._strip_currency_whitespace(None) is None
    assert Client._strip_currency_whitespace(np.nan) is np.nan


def test_drop_currency_symbol_columns_drops_only_all_symbol_columns():
    df = pd.DataFrame(
        {
            0: ["$", "$", "$"],
            1: ["12,900.00", "160,000.00", "17,700.00"],
            2: ["", None, "$"],
        }
    )

    result = Client._drop_currency_symbol_columns(df)

    assert list(result.columns) == [1]


def test_drop_index_column_removes_num_when_present():
    df = pd.DataFrame({"Num": [1, 2], "Club": ["LA", "SJ"]})
    assert list(Client._drop_index_column(df).columns) == ["Club"]


def test_drop_index_column_is_noop_when_absent():
    df = pd.DataFrame({"Club": ["LA", "SJ"]})
    assert list(Client._drop_index_column(df).columns) == ["Club"]


def test_rename_header_variants_maps_known_aliases_and_ignores_others():
    df = pd.DataFrame(columns=["Team", "Playing Position", "Salary", "Untouched"])
    result = Client._rename_header_variants(df)
    assert list(result.columns) == ["Club", "Pos", "Base Salary", "Untouched"]


def test_validate_intermediate_schema_accepts_any_column_order():
    df = pd.DataFrame(columns=list(reversed(_INTERMEDIATE_COLUMNS)))
    result = Client._validate_intermediate_schema(df)
    assert list(result.columns) == list(reversed(_INTERMEDIATE_COLUMNS))


def test_validate_intermediate_schema_rejects_mismatch():
    # missing "Compensation"
    df = pd.DataFrame(columns=["Club", "Last Name", "First Name", "Pos", "Base Salary"])
    with pytest.raises(ValueError, match="Expected intermediate columns"):
        Client._validate_intermediate_schema(df)


# --- _normalize_columns / club + currency normalization --------------------


def _intermediate_df(**overrides):
    base = {
        "Club": "LA",
        "Last Name": "Adzemian",
        "First Name": "Vardan",
        "Pos": "D",
        "Base Salary": "12,900.00",
        "Compensation": "$12,900.00",
    }
    base.update(overrides)
    return pd.DataFrame([base])


def test_normalize_columns_maps_known_club_alias():
    result = Client._normalize_columns(_intermediate_df(Club="ATL"))
    assert result.loc[0, "Club"] == "Atlanta United FC"


def test_normalize_columns_preserves_unrecognized_club_text():
    result = Client._normalize_columns(_intermediate_df(Club="Some New Expansion FC"))
    assert result.loc[0, "Club"] == "Some New Expansion FC"


def test_normalize_columns_blank_club_becomes_major_league_soccer():
    result = Client._normalize_columns(_intermediate_df(Club=None))
    assert result.loc[0, "Club"] == "Major League Soccer"


def test_normalize_columns_strips_whitespace_before_mapping():
    result = Client._normalize_columns(_intermediate_df(Club=" ATL "))
    assert result.loc[0, "Club"] == "Atlanta United FC"


def test_normalize_columns_parses_currency_regardless_of_dollar_sign():
    overrides = {"Base Salary": "12,900.00", "Compensation": "$12,900.00"}
    result = Client._normalize_columns(_intermediate_df(**overrides))
    assert result.loc[0, "Base Salary"] == 12900.0
    assert result.loc[0, "Guaranteed Compensation"] == 12900.0


def test_normalize_columns_undisclosed_salary_becomes_nan_not_an_error():
    overrides = {"Base Salary": "-", "Compensation": "$-"}
    result = Client._normalize_columns(_intermediate_df(**overrides))
    assert pd.isna(result.loc[0, "Base Salary"])
    assert pd.isna(result.loc[0, "Guaranteed Compensation"])


def test_normalize_columns_output_shape():
    result = Client._normalize_columns(_intermediate_df())
    assert list(result.columns) == [
        "First Name",
        "Last Name",
        "Club",
        "Position",
        "Base Salary",
        "Guaranteed Compensation",
    ]


# --- Alias-table invariants --------------------------------------------------
# These guard against copy/paste mistakes as the tables grow: an alias listed
# under two different canonical names would silently pick whichever happened
# to be processed last when the lookup dict is built.


def test_club_aliases_has_no_duplicate_alias_across_canonical_entries():
    all_aliases = [alias for aliases in _CLUB_ALIASES.values() for alias in aliases]
    assert len(all_aliases) == len(set(all_aliases))
    assert len(_CLUB_ALIAS_LOOKUP) == len(all_aliases)


def test_club_aliases_no_canonical_name_is_also_an_alias():
    canonical_names = set(_CLUB_ALIASES)
    assert canonical_names.isdisjoint(_CLUB_ALIAS_LOOKUP)


def test_header_aliases_has_no_duplicate_alias_across_canonical_entries():
    all_aliases = [alias for aliases in _HEADER_ALIASES.values() for alias in aliases]
    assert len(all_aliases) == len(set(all_aliases))
    assert len(_HEADER_ALIAS_LOOKUP) == len(all_aliases)


def test_header_aliases_canonical_names_are_all_intermediate_columns():
    assert set(_HEADER_ALIASES).issubset(_INTERMEDIATE_COLUMNS)


# --- Pipeline / fixup / merger registries -----------------------------------


def test_get_pipeline_falls_back_to_default_for_unregistered_year():
    assert _get_pipeline(2015) == mlspa_client._DEFAULT_STEPS


def test_get_pipeline_returns_override_for_registered_year():
    pipeline = _get_pipeline(2020)
    assert Client._fix_2020_salary_column in pipeline


def test_get_page_fixup_falls_back_to_identity():
    pages = [pd.DataFrame({"a": [1]})]
    assert _get_page_fixup(2015)(pages) == pages


def test_get_page_fixup_returns_registered_override():
    assert _get_page_fixup(2016) is Client._fix_2016_last_page


def test_get_page_merger_falls_back_to_repeated_header_stripper():
    assert _get_page_merger(2015) is Client._strip_repeated_page_headers


def test_get_page_merger_returns_registered_override():
    assert _get_page_merger(2018) is Client._strip_single_header_row


# --- CLI argument parsing ----------------------------------------------------


def test_parse_args_defaults_to_most_recent_year():
    args = _parse_args([])
    assert args.years == [max(_SALARY_REPORTS)]


def test_parse_args_accepts_single_year():
    assert _parse_args(["2024"]).years == [2024]


def test_parse_args_accepts_multiple_years():
    assert _parse_args(["2020", "2021", "2022"]).years == [2020, 2021, 2022]


def test_parse_args_rejects_non_integer_year():
    with pytest.raises(SystemExit):
        _parse_args(["not-a-year"])


# --- End-to-end fetch() (network + tabula mocked out) -----------------------


def test_fetch_unsupported_year_is_non_fatal_and_makes_no_request(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("requests.get should not be called for an unsupported year")

    monkeypatch.setattr(mlspa_client.requests, "get", fail_if_called)

    Client().fetch(1999)  # should not raise


def test_fetch_csv_era_report_end_to_end(monkeypatch):
    csv_bytes = (
        b"First Name,Last Name,Team Name,Position,PA Base Salary,Guaranteed Comp\n"
        b'Aleksey,Miranchuk,Atlanta United,Attacking Midfield,"$3,600,000.00","$4,885,441.00"\n'
    )
    monkeypatch.setattr(mlspa_client.requests, "get", lambda url: _FakeResponse(csv_bytes))
    calls = _capture_to_csv(monkeypatch)

    Client().fetch(2025)

    assert len(calls) == 1
    written = calls[0]["df"]
    assert list(written.columns) == [
        "First Name",
        "Last Name",
        "Club",
        "Position",
        "Base Salary",
        "Guaranteed Compensation",
    ]
    assert written.loc[0, "Club"] == "Atlanta United FC"
    assert written.loc[0, "Base Salary"] == 3600000.0
    assert calls[0]["kwargs"]["index"] is False


def test_fetch_pdf_era_report_end_to_end(monkeypatch):
    page = pd.DataFrame(
        [
            ["", None, None, None, None, 2015, "2015 Guaranteed"],
            ["Club", "Last Name", "First Name", "Pos", None, "Base Salary", "Compensation"],
            ["LA", "Adzemian", "Vardan", "D", "$", "12,900.00", "$12,900.00"],
        ]
    )
    monkeypatch.setattr(mlspa_client.requests, "get", lambda url: _FakeResponse(b"%PDF- fake"))
    monkeypatch.setattr(mlspa_client.tb, "read_pdf", lambda *args, **kwargs: [page])
    calls = _capture_to_csv(monkeypatch)

    Client().fetch(2015)

    assert len(calls) == 1
    written = calls[0]["df"]
    assert written.loc[0, "Club"] == "LA Galaxy"
    assert written.loc[0, "Base Salary"] == 12900.0
    assert written.loc[0, "Guaranteed Compensation"] == 12900.0
