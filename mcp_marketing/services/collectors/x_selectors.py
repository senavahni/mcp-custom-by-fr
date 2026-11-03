"""Selector DOM hasil pencarian X (x.com). UBAH DI SINI saja bila UI X berubah.

Parser (`x_browser.parse_search_html`) hanya memakai konstanta ini; test parser memakai fixture
`tests/fixtures/x_search.html` sehingga perubahan selector cepat diverifikasi.
"""

ARTICLE = 'article[data-testid="tweet"]'
STATUS_LINK = 'a[href*="/status/"]'
TIME = "time[datetime]"
TEXT = 'div[data-testid="tweetText"]'
USER_NAME = 'div[data-testid="User-Name"] a[href^="/"]'
SOCIAL_CONTEXT = 'span[data-testid="socialContext"], div[data-testid="socialContext"]'
REPLY = 'button[data-testid="reply"]'
REPOST = 'button[data-testid="retweet"], button[data-testid="unretweet"]'
LIKE = 'button[data-testid="like"], button[data-testid="unlike"]'
VIEWS = 'a[href$="/analytics"]'
REPLYING_TO_MARKERS = ("Replying to", "Membalas")
REPOST_MARKERS = ("reposted", "memposting ulang", "Retweeted")
LOGIN_URL_MARKERS = ("/login", "/i/flow/login", "/account/access")
