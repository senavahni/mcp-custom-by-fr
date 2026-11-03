"""Selector DOM pencarian Threads (threads.net). UBAH DI SINI saja bila UI Threads berubah.

Class CSS Threads diacak (obfuscated), jadi parser bertumpu pada atribut yang lebih stabil:
kontainer `data-pressable-container`, permalink `/@user/post/<code>`, `time[datetime]`, dan
`aria-label` ikon interaksi.
"""

POST_CONTAINER = 'div[data-pressable-container="true"]'
PERMALINK = 'a[href*="/post/"]'
TIME = "time[datetime]"
TEXT_SPANS = 'span[dir="auto"]'
LIKE_LABELS = ("Like", "Suka", "Unlike", "Batal suka")
REPLY_LABELS = ("Reply", "Balas", "Comment", "Komentar")
REPOST_LABELS = ("Repost", "Posting ulang")
LOGIN_URL_MARKERS = ("/login",)
