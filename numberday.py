"""Number of the Day: four different digits, eight guesses.

Bulls and Cows, the pencil game Mastermind was made from. The code is the
same on every node for the fleet's day. After each guess: a bull is a right
digit in the right place, a cow is a right digit in the wrong place.

    1234 1B 2C

Eight guesses and the prompt have to share one packet, so the feedback is
that short; the rules say what B and C stand for.
"""
import hashlib
import sys

import door_kit

GAME_ID = 'numberday'
COMMAND = 'NUMBERDAY'
NAME = 'Number of the Day'
MAX_GUESSES = 8
LENGTH = 4
PROMPT = "Send 4 different digits."

RULES = ("Find the 4-digit code in 8 tries. Every digit is different. After "
         "each guess: B (bull) = right digit, right place; C (cow) = right "
         "digit, wrong place. Same code on every node, new each day.")


def answer(day: str) -> str:
    """Four different digits from the fleet's date.

    Ordered by a hash of each digit with the day, not shuffled with the
    random module: the nodes run different Python versions, and all of them
    have to arrive at the same code.
    """
    def rank(digit):
        return hashlib.sha256(f"{GAME_ID}:{day}:{digit}".encode('ascii')).digest()
    return "".join(sorted("0123456789", key=rank)[:LENGTH])


def normalise(text: str) -> str:
    guess = text.strip().replace(' ', '')
    if len(guess) != LENGTH or not guess.isascii() or not guess.isdigit():
        raise ValueError(f"A guess is {LENGTH} digits.")
    if len(set(guess)) != LENGTH:
        raise ValueError("Every digit must be different.")
    return guess


def count(target: str, guess: str) -> tuple:
    bulls = sum(want == got for want, got in zip(target, guess))
    cows = len(set(target) & set(guess)) - bulls
    return bulls, cows


def feedback(target: str, guess: str) -> str:
    bulls, cows = count(target, guess)
    return f"{guess} {bulls}B {cows}C"


def show(code: str) -> str:
    return code


def handle(user_id, text, short_name, nav=None):
    return door_kit.daily_handle(sys.modules[__name__], user_id, text, short_name)
