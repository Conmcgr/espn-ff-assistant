from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from espn_ff_assistant.opportunities import Opportunity, candidates, diff

NOW = datetime(2026, 10, 1, tzinfo=UTC)
ITEM = Opportunity('starter_out', 'key', 1, 2, 'mRoster', NOW + timedelta(hours=1), {'slot': 0})


def test_diff_lifecycle():
    assert diff({}, {'key': ITEM}, NOW) == [(ITEM, 'new')]
    previous = {'key': (ITEM, None)}
    assert diff(previous, {'key': ITEM}, NOW) == [(ITEM, 'unchanged')]
    changed = replace(ITEM, attrs={'slot': 2})
    assert diff(previous, {'key': changed}, NOW) == [(changed, 'changed')]
    assert diff(previous, {}, NOW) == [(ITEM, 'expired')]
    assert diff(previous, {'key': ITEM}, ITEM.expires_at) == [(ITEM, 'expired')]
    assert diff({'key': (ITEM, NOW)}, {'key': ITEM}, NOW) == []


def test_empty_and_already_expired():
    assert candidates({}, [], [], {}, 1, NOW) == {}
    assert diff({}, {}, NOW) == []
    assert diff({}, {'key': ITEM}, ITEM.expires_at) == []


def test_candidate_qualification_and_missing_data():
    status = SimpleNamespace(availability='waivers', waiver_clear_at=None,
                             pro_team_id=3, injury_status='OUT')
    entry = SimpleNamespace(provider_player_id=1, provider_team_id=2, lineup_slot_id=0)
    game = SimpleNamespace(kickoff_at=NOW + timedelta(hours=1), is_bye=False)
    drops = [('tx', 1, 2, NOW - timedelta(hours=1))]
    current = candidates({1: status}, [entry], drops, {3: game}, 4, NOW)
    assert set(current) == {'player_dropped:tx:1', 'starter_out:2:1:4'}
    status.injury_status = 'INJURY_RESERVE'
    updated = candidates({1: status}, [entry], drops, {3: game}, 4, NOW)
    assert [change for _, change in diff({k: (v, None) for k, v in current.items()}, updated, NOW)] == [
        'unchanged', 'changed',
    ]
    assert candidates({}, [entry], drops, {}, 4, NOW) == {}
    assert candidates({1: status}, [entry], [('tx', 1, 2, None)], {}, 4, NOW) == {}
    assert candidates({1: status}, [entry], [('tx', 1, 2, NOW + timedelta(days=1))], {}, 4, NOW) == {}
    entry.lineup_slot_id = 20
    assert candidates({1: status}, [entry], [], {3: game}, 4, NOW) == {}
    entry.lineup_slot_id = 0
    status.injury_status = 'ACTIVE'
    status.availability = 'rostered'
    assert candidates({1: status}, [entry], drops, {3: game}, 4, NOW) == {}
