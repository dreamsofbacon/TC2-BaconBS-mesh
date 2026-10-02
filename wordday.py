"""Word of the Day: five letters, six guesses, the same word on every node.

The answer comes from the fleet's date, so nothing has to be synced for two
people on different nodes to be working on the same puzzle. Feedback has no
colour to lean on, so each guess is shown again in a second form: a capital
is the right letter in the right place, a small letter is in the word but
elsewhere, and a dot is not in the word.

    CRANE  C . a . e

Answers are everyday words, listed here. Guesses are checked against the
public-domain ENABLE word list in data/words5.txt, so a guess has to be a
word but need not be a common one.
"""
import os
import sys

import door_kit

GAME_ID = 'wordday'
COMMAND = 'WORDDAY'
NAME = 'Word of the Day'
MAX_GUESSES = 6
LENGTH = 5
PROMPT = "Send a 5-letter word."

RULES = ("Guess the 5-letter word in 6 tries. After each guess: a CAPITAL is "
         "right letter, right place; a small letter is in the word, wrong "
         "place; a dot is not in it. Same word on every node, new each day.")

ANSWERS = tuple("""
about above actor adult after again agree ahead alarm album alert alive allow
alone along among angle angry ankle apple apron arrow aside audio avoid awake
award aware bacon badge baker basic beach beard beast begin being below bench
berry birth black blade blame blank blast blaze blend blind block bloom board
boast bonus boost booth brain brake brand brave bread break brick bride brief
bring broad brook brown brush build bunch cabin cable camel canal candy cargo
carry catch cause chain chair chalk charm chart chase cheap check cheek cheer
chess chest chief child chill claim class clean clear clerk click cliff climb
clock close cloth cloud coach coast color comet coral couch count court cover
crack craft crane crash crate crazy cream creek crest crisp cross crowd crown
crumb crust curve cycle daily dairy dance delay depth diary diner dizzy dodge
doubt dough draft drain drama dream dress drift drill drink drive eager eagle
early earth eight elbow empty enjoy enter equal error event every exact exist
extra fable faint fairy faith false fancy feast fence ferry fever field fiery
fifty final first flame flash fleet float flock flood floor flour fluid flute
focus force forge forty found frame fresh front frost fruit funny giant given
glass globe glove grace grade grain grand grant grape graph grass great green
greet grill group guard guess guest guide habit happy harsh haste heart heavy
hedge hello honey horse hotel house human humor ideal image index inner input
ivory jelly jewel joint judge juice knife knock label large laser later laugh
layer learn least leave lemon level light limit linen local logic loose lucky
lunch magic major maker maple march match maybe mayor medal melon mercy merit
metal meter might minor mixer model money month moral motor mound mouse mouth
movie music naval nerve never night noble noise north novel nurse ocean offer
often olive onion opera orbit order other outer owner paint panel paper party
pasta patch pause peace peach pearl pedal penny phone photo piano piece pilot
pitch pixel pizza place plain plane plant plate plaza point polar porch pound
power press price pride prime print prize proof proud pulse punch pupil quart
queen quick quiet quilt quite radar radio rainy raise ranch range rapid reach
ready relay reply ridge rifle right river roast robin robot rocky rough round
route royal rugby ruler rural salad sauce scale scarf scene scent scoop score
scout seven shade shake shape share sharp sheep sheet shelf shell shift shine
shirt shore short shout sight since sixty skate skill slice slide slope
small smart smile smoke snack snake solar solid sound south space spare spark
speak speed spice spike spoon sport spray squad stack stage stair stamp stand
start state steam steel steep stick still stock stone store storm story stove
straw study style sugar sunny super sweet swift swing table taste teach thank
their theme there thick thing think third three throw thumb tiger tight timer
tired title toast today token tooth topic torch total touch tower track trade
trail train treat trend trial tribe trick truck trust truth tulip twice twist
uncle under union unity until upper urban usual valid value video visit vital
vivid voice wagon watch water weary wheat wheel where which while white whole
woman world worry worth would wrist write wrong young youth zebra
""".split())

_ALLOWED = None


def allowed_words() -> frozenset:
    """Every word a guess may be: the dictionary, plus the answers in case
    the file is missing. Read once."""
    global _ALLOWED
    if _ALLOWED is None:
        words = set(ANSWERS)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'words5.txt')
        try:
            with open(path, encoding='ascii') as handle:
                words.update(line.strip() for line in handle if len(line.strip()) == LENGTH)
        except OSError:
            pass
        _ALLOWED = frozenset(words)
    return _ALLOWED


def answer(day: str) -> str:
    return ANSWERS[door_kit.daily_seed(GAME_ID, day) % len(ANSWERS)]


def normalise(text: str) -> str:
    guess = text.strip().lower()
    if len(guess) != LENGTH or not guess.isascii() or not guess.isalpha():
        raise ValueError(f"A guess is {LENGTH} letters.")
    if guess not in allowed_words():
        raise ValueError(f"{guess.upper()} is not in the word list.")
    return guess


def marks(target: str, guess: str) -> str:
    """One character per letter: the capital, the small letter, or a dot.

    A letter is only marked "elsewhere" as many times as the answer has it
    left over, so a guess with two Es against an answer with one shows one.
    """
    out = ['.'] * LENGTH
    spare = {}
    for index, (want, got) in enumerate(zip(target, guess)):
        if want == got:
            out[index] = got.upper()
        else:
            spare[want] = spare.get(want, 0) + 1
    for index, got in enumerate(guess):
        if out[index] == '.' and spare.get(got, 0):
            out[index] = got
            spare[got] -= 1
    return "".join(out)


def feedback(target: str, guess: str) -> str:
    return f"{guess.upper()}  {' '.join(marks(target, guess))}"


def show(word: str) -> str:
    return word.upper()


def handle(user_id, text, short_name, nav=None):
    return door_kit.daily_handle(sys.modules[__name__], user_id, text, short_name)
