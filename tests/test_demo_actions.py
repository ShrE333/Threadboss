from app.demo_actions import BLR_TO_PNQ_URL, PNQ_TO_BLR_URL, match_demo_flight_action


def test_pune_to_bangalore_exact_demo_sentence():
    action = match_demo_flight_action('Book me a ticket from Pune to Bangalore on 15 September 2026')
    assert action is not None
    assert action.url == PNQ_TO_BLR_URL


def test_pune_to_banglore_typo_is_supported():
    action = match_demo_flight_action('book me a flight from pune to banglore on 15 sept')
    assert action is not None
    assert action.url == PNQ_TO_BLR_URL


def test_bangalore_to_pune_exact_demo_sentence():
    action = match_demo_flight_action('Book me a ticket from Bangalore to Pune on 15 September 2026')
    assert action is not None
    assert action.url == BLR_TO_PNQ_URL


def test_non_demo_travel_question_is_not_hardcoded():
    assert match_demo_flight_action('What flights are available to Bangalore tomorrow?') is None
