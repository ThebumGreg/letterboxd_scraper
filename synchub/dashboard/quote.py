"""One line a day for the bottom of the page: a quote (ZenQuotes), a dad joke, a fun fact or "this day in history", or a rotation of them. The internet is
used when it is there, else the built-in lists. The same line is kept all day."""
import json
import zlib
from datetime import date

from synchub import http, settings

BUILT_IN = (
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("Well done is better than well said.", "Benjamin Franklin"),
    ("The best way to predict the future is to create it.", "Peter Drucker"),
    ("Do what you can, with what you have, where you are.", "Theodore Roosevelt"),
    ("Whether you think you can or you think you can't, you're right.", "Henry Ford"),
    ("You miss 100% of the shots you don't take.", "Wayne Gretzky"),
    ("A journey of a thousand miles begins with a single step.", "Lao Tzu"),
    ("Act as if what you do makes a difference. It does.", "William James"),
    ("Small deeds done are better than great deeds planned.", "Peter Marshall"),
    ("Today is a good day to have a good day.", "Unknown"),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    ("Energy and persistence conquer all things.", "Benjamin Franklin"),
    ("What you do today can improve all your tomorrows.", "Ralph Marston"),
    ("Start where you are. Use what you have. Do what you can.", "Arthur Ashe"),
    ("Quality is not an act, it is a habit.", "Aristotle"),
    ("The harder I work, the luckier I get.", "Samuel Goldwyn"),
    ("Be the change that you wish to see in the world.", "Mahatma Gandhi"),
    ("Everything you've ever wanted is on the other side of fear.", "George Addair"),
    ("Don't watch the clock; do what it does. Keep going.", "Sam Levenson"),
    ("Believe you can and you're halfway there.", "Theodore Roosevelt"),
    ("Opportunities don't happen. You create them.", "Chris Grosser"),
    ("Happiness is not by chance, but by choice.", "Jim Rohn"),
    ("Kindness is a language the deaf can hear and the blind can see.", "Mark Twain"),
    ("Your attitude, not your aptitude, will determine your altitude.", "Zig Ziglar"),
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("Success is the sum of small efforts repeated day in and day out.", "Robert Collier"),
    ("Courage is not the absence of fear, but the triumph over it.", "Nelson Mandela"),
    ("Make each day your masterpiece.", "John Wooden"),
    ("Learn as if you will live forever, live as if you will die tomorrow.", "Mahatma Gandhi"),
)


JOKES = (
    ("I only know 25 letters of the alphabet. I don't know y.", ""),
    ("Why don't scientists trust atoms? Because they make up everything.", ""),
    ("I used to hate facial hair, but then it grew on me.", ""),
    ("What do you call a fake noodle? An impasta.", ""),
    ("Did you hear about the claustrophobic astronaut? He just needed a little space.", ""),
    ("Why did the scarecrow win an award? He was outstanding in his field.", ""),
    ("I'm reading a book about anti-gravity. It's impossible to put down.", ""),
    ("What do you call cheese that isn't yours? Nacho cheese.", ""),
    ("Why can't you give Elsa a balloon? She will let it go.", ""),
    ("I told my wife she was drawing her eyebrows too high. She looked surprised.", ""),
)
FACTS = (
    ("Honey never spoils: edible honey has been found in ancient Egyptian tombs.", ""),
    ("Octopuses have three hearts and blue blood.", ""),
    ("A day on Venus is longer than a year on Venus.", ""),
    ("Bananas are berries, but strawberries are not.", ""),
    ("The Eiffel Tower grows about 15 cm taller in summer because the iron expands in the heat.", ""),
    ("A group of flamingos is called a flamboyance.", ""),
    ("Sharks existed before trees did.", ""),
    ("There are more stars in the universe than grains of sand on all of Earth's beaches.", ""),
    ("Wombat droppings are cube shaped.", ""),
    ("Your stomach gets a new lining every few days.", ""),
)
HISTORY = (
    ("1969: Apollo 11 landed the first people on the Moon.", ""),
    ("1903: The Wright brothers made the first powered airplane flights at Kitty Hawk.", ""),
    ("1928: Alexander Fleming noticed the mold that led to penicillin.", ""),
    ("1989: The Berlin Wall opened.", ""),
    ("1492: Columbus reached the Caribbean.", ""),
    ("1876: Alexander Graham Bell made the first telephone call.", ""),
)
KINDS = ("quote", "joke", "fact", "history")
LABELS = {"quote": "", "joke": "Dad joke", "fact": "Fun fact", "history": "On this day"}
_LISTS = {"quote": BUILT_IN, "joke": JOKES, "fact": FACTS, "history": HISTORY}


def kind_for(day, source=None):
    """quote / joke / fact / history for this day: the chosen source, or a rotation when it is 'rotate'."""
    source = source or settings.get("dash_quote_source")
    return KINDS[day.toordinal() % len(KINDS)] if source == "rotate" else (source if source in KINDS else "quote")


def fallback(day, kind="quote"):
    text, author = _LISTS[kind][zlib.crc32(day.isoformat().encode()) % len(_LISTS[kind])]
    return {"text": text, "author": author, "source": "built-in", "kind": kind, "label": LABELS[kind]}


def _get(kind, url, **kw):
    res = http.request("quote", kind, "GET", url, timeout=8, **kw)
    return res.json() if res.status_code == 200 else None


def _fetch(kind="quote", day=None):
    if kind == "joke":
        body = _get("joke", "https://icanhazdadjoke.com/", headers={"Accept": "application/json", "User-Agent": "SyncHub family dashboard"})
        text, author, source = str((body or {}).get("joke") or "").strip(), "", "icanhazdadjoke.com"
    elif kind == "fact":
        body = _get("fact", "https://uselessfacts.jsph.pl/api/v2/facts/today", params={"language": "en"})
        text, author, source = str((body or {}).get("text") or "").strip(), "", "uselessfacts.jsph.pl"
    elif kind == "history":
        body = _get("history", "https://history.muffinlabs.com/date")
        events = [e for e in ((body or {}).get("data") or {}).get("Events") or [] if e.get("text") and len(str(e["text"])) <= 150]
        pick = events[zlib.crc32((day or date.today()).isoformat().encode()) % len(events)] if events else {}
        text, author, source = (f'{pick["year"]}: {pick["text"]}' if pick else ""), "", "history.muffinlabs.com"
    else:
        res = http.request("quote", "today", "GET", "https://zenquotes.io/api/today", timeout=8)
        if res.status_code != 200:
            return None
        first = (res.json() or [{}])[0]
        text, author, source = str(first.get("q") or "").strip(), str(first.get("a") or "").strip(), "zenquotes.io"
    # ZenQuotes answers with a rate-limit sentence in the same shape when it is asked too often
    if not text or len(text) > 300 or "too many requests" in text.lower() or author.lower() == "zenquotes.io":
        return None
    return {"text": text, "author": author, "source": source, "kind": kind, "label": LABELS[kind]}


def today(day=None, source=None):
    """-> {"text", "author", "source", "kind", "label"}. Never raises."""
    day = day or date.today()
    kind = kind_for(day, source)
    try:
        saved = json.loads(settings.get("dash_quote") or "{}")
    except ValueError:
        saved = {}
    if saved.get("date") == day.isoformat() and saved.get("text") and saved.get("kind", "quote") == kind and (saved.get("source") != "built-in" or saved.get("tried_at", 0) + 1800 > _now()):
        return saved
    quote = None
    try:
        quote = _fetch(kind, day)
    except Exception:
        pass
    quote = quote or fallback(day, kind)
    quote.update(date=day.isoformat(), tried_at=_now())
    settings.set_value("dash_quote", json.dumps(quote))
    return quote


def _now():
    import time
    return int(time.time())
