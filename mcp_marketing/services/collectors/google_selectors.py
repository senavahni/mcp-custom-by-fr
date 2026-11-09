"""Selector DOM Google Search & Google Maps (MCP-V1-06). UBAH DI SINI saja bila UI Google berubah.

Parser (`google_discovery`) hanya memakai konstanta ini; test parser memakai fixture
`tests/fixtures/google_*.html` sehingga perubahan selector cepat diverifikasi.
"""

# ---- Google Search (hasil organik; iklan berada di #tads/#bottomads, di luar #rso)
SEARCH_RESULTS_ROOT = "#rso"
SEARCH_RESULT_LINK = "a[href]"  # tautan organik = <a> yang memuat <h3>
SEARCH_RESULT_TITLE = "h3"
SEARCH_RESULT_SNIPPET = "div.VwiC3b, div[data-sncf], span.st"
SEARCH_ADS = "#tads, #tadsb, #bottomads, [data-text-ad]"

# ---- Google Maps: daftar hasil (feed) & halaman tempat
MAPS_FEED = 'div[role="feed"]'
MAPS_PLACE_LINK = 'a[href*="/maps/place/"]'
MAPS_NAME = "h1"
MAPS_CATEGORY = 'button[jsaction*="category"]'
MAPS_ADDRESS = 'button[data-item-id="address"]'
MAPS_PHONE = 'button[data-item-id^="phone:tel:"]'
MAPS_WEBSITE = 'a[data-item-id="authority"]'
MAPS_RATING = 'div.F7nice span[aria-hidden="true"]'
MAPS_REVIEW_COUNT = 'div.F7nice span[aria-label*="ulasan"], div.F7nice span[aria-label*="review"]'
# label aksesibilitas tombol diawali "Alamat: " / "Telepon: " (UI id) atau "Address: " (en)
LABEL_PREFIXES = ("Alamat:", "Address:", "Telepon:", "Phone:", "Situs Web:", "Website:")

# ---- halaman blokir / verifikasi (tidak pernah diakali: job pause, user menyelesaikan manual)
CAPTCHA_URL_MARKERS = ("/sorry/", "google.com/sorry", "/recaptcha/")
CAPTCHA_HTML_MARKERS = (
    'id="captcha-form"',
    "g-recaptcha",
    "unusual traffic from your computer network",
    "traffic tidak biasa dari jaringan komputer",
)
CONSENT_URL_MARKERS = ("consent.google.com", "consent.youtube.com")
