import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dopewars
import dopewars_menu
import dopewars_theme


def test_status_uses_requested_three_line_layout_and_net_money():
    state = dopewars.new_game(1)
    state.update(cash=2400, debt=1200, loan_due=30, bank=500, hp=100)

    assert dopewars_menu._status(state, dopewars_theme.theme(True)) == (
        "⏱️ D1/30 | 📍 Bronx | 💵 $2400 | 💳 $1200 (due D30)\n"
        "❤️ 100 | 🎒 0/40 | Gear — | Net $1700"
    )


def test_status_shows_owned_gear_icons_and_heroin_uses_syringe():
    state = dopewars.new_game(1)
    state.update(weapon=1, armor=1)

    status = dopewars_menu._status(state, dopewars_theme.theme(True))
    assert "Gear 🔫🛡️ | Net " in status
    assert dopewars.GOOD_ICONS["weed"] == "🌿"
    assert dopewars.GOOD_ICONS["heroin"] == "💉"
