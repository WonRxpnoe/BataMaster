import logging

import pandas as pd
import pytest

from rank_income_by_zip import DEFAULT_OUTPUT, load_income, main, parse_args


@pytest.fixture(autouse=True)
def capture_logs(caplog):
    caplog.set_level(logging.DEBUG, logger='rank_income_by_zip')
    return caplog


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Write an IRS style file and return a helper that fills in one row at a time."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'irs.csv'

    def write(*rows):
        frame = pd.DataFrame(
            [
                {'STATEFIPS': '06', 'STATE': state, 'ZIPCODE': zip_code,
                 'agi_stub': 0, 'N1': returns, 'A00100': total_agi}
                for zip_code, state, returns, total_agi in rows
            ]
        )
        frame.to_csv(path, index=False)
        return str(path)

    return write


class TestLoadIncome:
    def test_divides_total_income_by_the_number_of_returns(self, source):
        # The IRS reports income in thousands, so 1,000,000 over 2,000 returns is $500,000
        path = source(('90210', 'CA', 2000, 1000000))

        assert load_income(path).loc[0, 'avg_income'] == 500000

    def test_ranks_the_highest_average_first(self, source):
        path = source(
            ('11111', 'CA', 1000, 100000),
            ('22222', 'CA', 1000, 900000),
            ('33333', 'CA', 1000, 500000),
        )

        ranked = load_income(path)
        assert ranked['zip_code'].tolist() == ['22222', '33333', '11111']
        assert ranked['rank'].tolist() == [1, 2, 3]

    @pytest.mark.parametrize('aggregate', ['00000', '99999'])
    def test_drops_the_rows_that_total_a_whole_state(self, source, aggregate):
        path = source(
            (aggregate, 'CA', 2000000, 9000000000),
            ('90210', 'CA', 1000, 100000),
        )

        assert load_income(path)['zip_code'].tolist() == ['90210']

    def test_drops_zip_codes_with_too_few_returns_to_be_stable(self, source):
        # One large return would otherwise put a near empty zip code at the top
        path = source(
            ('00001', 'CA', 5, 500000),
            ('90210', 'CA', 1000, 100000),
        )

        assert load_income(path, min_returns=200)['zip_code'].tolist() == ['90210']

    def test_the_threshold_can_be_lowered(self, source):
        path = source(('00001', 'CA', 5, 500000))

        assert len(load_income(path, min_returns=1)) == 1

    def test_keeps_the_leading_zero_of_an_east_coast_zip(self, source):
        path = source(('07078', 'NJ', 1000, 100000))

        assert load_income(path).loc[0, 'zip_code'] == '07078'


class TestCommandLine:
    def test_defaults(self):
        args = parse_args([])
        assert args.output == DEFAULT_OUTPUT
        assert args.state is None

    def test_writes_the_ranking_to_a_csv(self, source, tmp_path):
        path = source(('90210', 'CA', 1000, 500000), ('94301', 'CA', 1000, 900000))

        assert main(['--source', path, '--output', 'out.csv']) == 0

        written = pd.read_csv(tmp_path / 'out.csv', dtype={'zip_code': str})
        assert written['zip_code'].tolist() == ['94301', '90210']

    def test_reports_a_missing_source_instead_of_crashing(self, tmp_path, capture_logs):
        assert main(['--source', str(tmp_path / 'absent.csv')]) == 1
        assert 'Missing' in capture_logs.text

    def test_creates_a_missing_output_directory(self, source, tmp_path):
        path = source(('90210', 'CA', 1000, 500000))

        main(['--source', path, '--output', 'nested/out.csv'])

        assert (tmp_path / 'nested/out.csv').is_file()

    def test_the_state_filter_only_narrows_what_is_printed(self, source, tmp_path, capsys):
        path = source(('90210', 'CA', 1000, 500000), ('07078', 'NJ', 1000, 900000))

        main(['--source', path, '--output', 'out.csv', '--state', 'CA'])

        assert '90210' in capsys.readouterr().out
        # The file still holds every state, so it can be filtered again later
        assert len(pd.read_csv(tmp_path / 'out.csv')) == 2
