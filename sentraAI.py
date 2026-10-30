
import os
import json
import time
import threading
import re
import html
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

# ==========================================
# COINSENTRA AI 5.0
# ==========================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

POOLS_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/new_pools"
)

NEWS_FEEDS = [
    ("CoinDesk",
     "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Cointelegraph",
     "https://cointelegraph.com/rss"),
    ("Decrypt",
     "https://decrypt.co/feed"),
]

POOL_REFRESH = 120
NEWS_REFRESH = 300
TIMEOUT = 15

lock = threading.Lock()

data = {
    "pools": [],
    "news": [],
    "pool_updated": None,
    "news_updated": None,
    "pool_error": None,
    "news_error": None
}


# ==========================================
# HTTP DATA
# ==========================================

def get_data(url):
    request = Request(
        url,
        headers={
            "User-Agent": "CoinSentraAI/5.0",
            "Accept": "*/*"
        }
    )

    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def clean(value):
    return html.unescape(
        re.sub(r"<[^>]*>", " ", str(value or ""))
    ).strip()


def parse_date(value):
    if not value:
        return None

    try:
        date = parsedate_to_datetime(value)

        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        return date.astimezone(timezone.utc).isoformat()

    except Exception:
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).astimezone(timezone.utc).isoformat()
        except Exception:
            return None


# ==========================================
# OPPORTUNITY SCORE
# ==========================================

def calculate_score(liquidity, volume, age, buys, sells):

    score = 35

    if liquidity >= 50000:
        score += 22
    elif liquidity >= 10000:
        score += 16
    elif liquidity >= 3000:
        score += 10
    elif liquidity >= 1000:
        score += 5
    else:
        score -= 12

    if volume >= 100000:
        score += 18
    elif volume >= 20000:
        score += 12
    elif volume >= 5000:
        score += 7
    elif volume < 500:
        score -= 8

    if 1 <= age <= 24:
        score += 10
    elif age < 1:
        score += 3
    elif age > 168:
        score -= 5

    total = buys + sells

    if total > 0:
        ratio = buys / total

        if ratio >= 0.65:
            score += 8
        elif ratio <= 0.35:
            score -= 8

    return max(0, min(100, int(score)))


# ==========================================
# SOLANA TOKEN SCANNER
# ==========================================

def update_pools():

    try:
        response = get_data(POOLS_URL)

        result = json.loads(
            response.decode("utf-8", "replace")
        )

        now = datetime.now(timezone.utc)
        pools = []

        for item in result.get("data", []):

            attributes = item.get("attributes") or {}

            name = clean(attributes.get("name"))
            address = attributes.get("address", "")

            liquidity = number(
                attributes.get("reserve_in_usd")
            )

            volume_data = attributes.get("volume_usd") or {}
            volume = number(volume_data.get("h24"))

            transactions = (
                attributes.get("transactions") or {}
            ).get("h24") or {}

            buys = int(number(transactions.get("buys")))
            sells = int(number(transactions.get("sells")))

            age = 9999

            created = attributes.get("pool_created_at")

            if created:
                try:
                    created_date = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )

                    age = max(
                        0,
                        (now - created_date).total_seconds() / 3600
                    )

                except Exception:
                    pass

            score = calculate_score(
                liquidity,
                volume,
                age,
                buys,
                sells
            )

            risk = max(
                0,
                min(
                    100,
                    100 - score + (15 if liquidity < 1000 else 0)
                )
            )

            if risk >= 60:
                risk_label = "HIGH"
            elif risk >= 35:
                risk_label = "MEDIUM"
            else:
                risk_label = "LOW"

            pools.append({
                "name": name or "Unknown pool",
                "address": address,
                "liquidity": round(liquidity, 2),
                "volume24": round(volume, 2),
                "buys24": buys,
                "sells24": sells,
                "ageHours": round(age, 1) if age < 9999 else None,
                "score": score,
                "risk": risk,
                "riskLabel": risk_label,
                "dexUrl": (
                    "https://dexscreener.com/solana/"
                    + quote(address, safe="")
                )
            })

        pools.sort(
            key=lambda pool: pool["score"],
            reverse=True
        )

        with lock:
            data["pools"] = pools[:40]
            data["pool_updated"] = datetime.now(
                timezone.utc
            ).isoformat()
            data["pool_error"] = None

        print("CoinSentra: Solana pools updated:", len(pools))

    except HTTPError as error:

        message = (
            "GeckoTerminal rate limit (429). Retrying later."
            if error.code == 429
            else f"Pool API HTTP error: {error.code}"
        )

        with lock:
            data["pool_error"] = message

        print(message)

    except Exception as error:

        with lock:
            data["pool_error"] = (
                "Pool data temporarily unavailable: "
                + str(error)[:120]
            )

        print("Pool error:", error)


# ==========================================
# CRYPTO NEWS SENTIMENT
# ==========================================

def analyze_sentiment(text):

    text = text.lower()

    positive_words = [
        "surge", "rally", "bullish", "gain",
        "approval", "adoption", "partnership",
        "growth", "breakout", "recovery",
        "record high", "inflow"
    ]

    negative_words = [
        "crash", "hack", "exploit", "lawsuit",
        "ban", "bearish", "drop", "loss",
        "scam", "outage", "liquidation",
        "fraud", "bankruptcy"
    ]

    positive = sum(
        word in text for word in positive_words
    )

    negative = sum(
        word in text for word in negative_words
    )

    if positive > negative:
        return "Positive"

    if negative > positive:
        return "Negative"

    return "Neutral"


# ==========================================
# CRYPTO NEWS RSS READER
# ==========================================

def update_news():

    news_items = []
    errors = []

    for source, url in NEWS_FEEDS:

        try:
            response = get_data(url)

            root = ET.fromstring(response)

            # Standard RSS feeds

            for item in root.findall(".//item"):

                title = clean(item.findtext("title"))
                link = (item.findtext("link") or "").strip()

                summary = clean(
                    item.findtext("description")
                )[:350]

                published = parse_date(
                    item.findtext("pubDate")
                    or item.findtext("date")
                )

                if title and link:

                    news_items.append({
                        "title": title[:220],
                        "summary": summary,
                        "link": link,
                        "source": source,
                        "published": published,
                        "sentiment": analyze_sentiment(
                            title + " " + summary
                        )
                    })

            # Atom feeds

            namespace = {
                "a": "http://www.w3.org/2005/Atom"
            }

            for item in root.findall(".//a:entry", namespace):

                title = clean(
                    item.findtext(
                        "a:title",
                        default="",
                        namespaces=namespace
                    )
                )

                summary = clean(
                    item.findtext(
                        "a:summary",
                        default="",
                        namespaces=namespace
                    )
                )[:350]

                link_element = item.find(
                    "a:link",
                    namespace
                )

                link = (
                    link_element.attrib.get("href", "")
                    if link_element is not None
                    else ""
                )

                published = parse_date(
                    item.findtext(
                        "a:published",
                        default="",
                        namespaces=namespace
                    )
                )

                if title and link:

                    news_items.append({
                        "title": title[:220],
                        "summary": summary,
                        "link": link,
                        "source": source,
                        "published": published,
                        "sentiment": analyze_sentiment(
                            title + " " + summary
                        )
                    })

        except Exception as error:

            errors.append(source)
            print("News source error:", source, error)

    # Remove duplicate headlines

    unique = []
    seen = set()

    for item in news_items:

        key = re.sub(
            r"[^a-z0-9]",
            "",
            item["title"].lower()
        )

        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(
        key=lambda item: item.get("published") or "",
        reverse=True
    )

    with lock:

        if unique:

            data["news"] = unique[:30]

            data["news_updated"] = datetime.now(
                timezone.utc
            ).isoformat()

            data["news_error"] = (
                "Some news sources unavailable."
                if errors else None
            )

        else:

            data["news_error"] = (
                "News feeds temporarily unavailable."
            )

    print("CoinSentra: News headlines:", len(unique))


# ==========================================
# BACKGROUND REFRESH
# ==========================================

def pool_loop():

    while True:
        update_pools()
        time.sleep(POOL_REFRESH)


def news_loop():

    while True:
        update_news()
        time.sleep(NEWS_REFRESH)


# ==========================================
# WEBSITE DESIGN
# ==========================================

PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#090817">

<title>CoinSentra AI - Crypto Intelligence</title>

<style>

:root {
--bg:#090817;
--panel:#15122a;
--line:#342b55;
--text:#f5f1ff;
--muted:#aaa2c3;
--purple:#a78bfa;
--cyan:#52e5e5;
--red:#ff637d;
--green:#54e0a0;
--yellow:#ffd166;
}

* { box-sizing:border-box; }

body {
margin:0;
background:radial-gradient(circle at 50% -20%,#30205d,#090817 50%);
color:var(--text);
font-family:Arial,sans-serif;
}

.wrap {
max-width:1200px;
margin:auto;
padding:20px;
}

header {
display:flex;
justify-content:space-between;
align-items:center;
gap:12px;
flex-wrap:wrap;
}

.brand {
font-size:26px;
font-weight:900;
}

.brand span {
color:var(--purple);
}

.muted {
color:var(--muted);
font-size:12px;
}

.pill,.tag {
border:1px solid var(--line);
border-radius:30px;
padding:7px 10px;
font-size:11px;
color:var(--cyan);
}

.hero {
margin:22px 0;
padding:24px;
border:1px solid #49377a;
border-radius:17px;
background:linear-gradient(115deg,#211640,#121126);
}

.hero h1 {
font-size:clamp(25px,5vw,40px);
margin:0 0 8px;
}

.hero p {
color:var(--muted);
line-height:1.5;
margin:0;
}

.stats {
display:grid;
grid-template-columns:repeat(4,1fr);
gap:10px;
}

.stat,.card {
background:var(--panel);
border:1px solid var(--line);
border-radius:14px;
padding:15px;
}

.stat small {
color:var(--muted);
font-size:10px;
text-transform:uppercase;
}

.stat strong {
display:block;
font-size:25px;
margin-top:8px;
}

.section {
display:flex;
justify-content:space-between;
align-items:center;
gap:10px;
flex-wrap:wrap;
margin:28px 0 12px;
}

.section h2 {
font-size:20px;
margin:0;
}

.controls {
display:flex;
gap:8px;
flex-wrap:wrap;
margin:12px 0;
}

button,select {
background:#211b3b;
color:var(--text);
border:1px solid var(--line);
border-radius:9px;
padding:9px 12px;
}

button { cursor:pointer; }

.active {
border-color:var(--purple);
background:#38275f;
}

.grid,.newsgrid {
display:grid;
grid-template-columns:repeat(2,1fr);
gap:12px;
}

.token,.news {
background:var(--panel);
border:1px solid var(--line);
border-radius:13px;
padding:15px;
min-width:0;
}

.head {
display:flex;
justify-content:space-between;
gap:8px;
}

.token h3,.news h3 {
font-size:15px;
margin:0 0 7px;
overflow-wrap:anywhere;
}

.token a,.news a {
color:var(--text);
text-decoration:none;
}

.tag { color:var(--purple); white-space:nowrap; }

.high { color:var(--red); background:#421c31; }
.medium { color:var(--yellow); background:#3c321b; }
.low { color:var(--green); background:#14372d; }

.metrics {
display:grid;
grid-template-columns:repeat(3,1fr);
gap:7px;
margin-top:12px;
}

.metric {
background:#0e0c1e;
border-radius:8px;
padding:9px;
}

.metric small {
display:block;
color:var(--muted);
font-size:10px;
}

.metric b {
display:block;
margin-top:5px;
font-size:13px;
}

.bar {
height:5px;
background:#302945;
border-radius:8px;
margin-top:12px;
overflow:hidden;
}

.bar i {
display:block;
height:100%;
background:linear-gradient(90deg,var(--purple),var(--cyan));
}

.news p {
font-size:12px;
line-height:1.5;
color:var(--muted);
}

.foot {
font-size:11px;
color:var(--muted);
margin-top:12px;
}

footer {
text-align:center;
color:var(--muted);
font-size:11px;
padding:28px 0;
line-height:1.8;
}

@media(max-width:700px) {
.stats { grid-template-columns:repeat(2,1fr); }
.grid,.newsgrid { grid-template-columns:1fr; }
.wrap { padding:13px; }
.hero { padding:18px; }
}

</style>
</head>

<body>

<main class="wrap">

<header>

<div>
<div class="brand">COINSENTRA <span>AI</span></div>
<div class="muted">
Crypto Intelligence • Solana Scanner • Market News
</div>
</div>

<div class="pill">● SYSTEM ONLINE</div>

</header>

<section class="hero">

<h1>See the signal. Understand the risk.</h1>

<p>
Track new Solana pools, review liquidity and trading activity,
and follow crypto headlines from multiple publishers
in one dashboard.
</p>

</section>

<section class="stats">

<div class="stat">
<small>Pools found</small>
<strong id="pc">—</strong>
</div>

<div class="stat">
<small>High-risk pools</small>
<strong id="hc">—</strong>
</div>

<div class="stat">
<small>Score 70+</small>
<strong id="gc">—</strong>
</div>

<div class="stat">
<small>News headlines</small>
<strong id="nc">—</strong>
</div>

</section>

<div class="section">
<h2>⚡ Solana New Token Scanner</h2>
<span class="muted" id="ps">Loading...</span>
</div>

<div class="controls" id="filters">

<button class="active" data-f="all">All pools</button>
<button data-f="good">Score 70+</button>
<button data-f="high">High risk</button>
<button data-f="liq">Liquidity $1k+</button>

<select id="sort">
<option value="score">Sort: AI score</option>
<option value="liq">Sort: Liquidity</option>
<option value="vol">Sort: 24h volume</option>
</select>

</div>

<div class="grid" id="pools">
<div class="card">Loading Solana pools...</div>
</div>

<div class="section">
<h2>📰 Latest Crypto News</h2>
<span class="muted" id="ns">Loading headlines...</span>
</div>

<div class="newsgrid" id="news">
<div class="card">Loading crypto news...</div>
</div>

<footer>

CoinSentra AI 5.0

<br>

Data: GeckoTerminal public API • CoinDesk • Cointelegraph • Decrypt

<br>

Scores and sentiment are automated indicators, not financial advice.
Always verify token contracts and news independently.

<br>

<a href="https://x.com/CoinSentraAI"
style="color:#a78bfa">
Follow CoinSentra AI on X
</a>

</footer>

</main>

<script>

let D = {pools:[],news:[]};
let filter = "all";

const esc = s => String(s || "").replace(/[&<>"']/g,c=>({
"&":"&amp;",
"<":"&lt;",
">":"&gt;",
'"':"&quot;",
"'":"&#39;"
}[c]));

const cash = n => "$" + Number(n || 0).toLocaleString(
"en-US",{maximumFractionDigits:0}
);

const age = n => {
if(n === null) return "—";
if(n < 1) return Math.round(n*60)+"m";
if(n < 48) return n.toFixed(1)+"h";
return (n/24).toFixed(1)+"d";
};


// TOKEN SCANNER

function renderPools() {

let items = [...D.pools];

if(filter === "good")
items = items.filter(x => x.score >= 70);

if(filter === "high")
items = items.filter(x => x.risk >= 60);

if(filter === "liq")
items = items.filter(x => x.liquidity >= 1000);

let sort = document.getElementById("sort").value;

items.sort((a,b) =>
sort === "liq" ? b.liquidity-a.liquidity :
sort === "vol" ? b.volume24-a.volume24 :
b.score-a.score
);

document.getElementById("pools").innerHTML =
items.length ? items.map(x => `

<article class="token">

<div class="head">

<div>
<h3>
<a href="${esc(x.dexUrl)}"
target="_blank" rel="noopener">
${esc(x.name)}
</a>
</h3>

<div class="muted">
${esc(x.address.slice(0,8))}...${esc(x.address.slice(-6))}
</div>

</div>

<span class="tag ${
x.risk>=60 ? "high" :
x.risk>=35 ? "medium" : "low"
}">
${x.riskLabel} RISK
</span>

</div>

<div class="metrics">

<div class="metric">
<small>AI opportunity</small>
<b>${x.score}/100</b>
</div>

<div class="metric">
<small>Liquidity</small>
<b>${cash(x.liquidity)}</b>
</div>

<div class="metric">
<small>24h volume</small>
<b>${cash(x.volume24)}</b>
</div>

<div class="metric">
<small>Pool age</small>
<b>${age(x.ageHours)}</b>
</div>

<div class="metric">
<small>24h buys</small>
<b>${x.buys24}</b>
</div>

<div class="metric">
<small>24h sells</small>
<b>${x.sells24}</b>
</div>

</div>

<div class="bar">
<i style="width:${x.score}%"></i>
</div>

<div class="foot">
Click token name to inspect on DexScreener
</div>

</article>

`).join("") :
'<div class="card">No pools match this filter.</div>';

}


// CRYPTO NEWS

function renderNews() {

document.getElementById("news").innerHTML =
D.news.length ? D.news.map(n => `

<article class="news">

<div class="head">

<span class="tag">
${esc(n.source)}
</span>

<span class="tag ${
n.sentiment === "Positive" ? "low" :
n.sentiment === "Negative" ? "high" :
"medium"
}">
${esc(n.sentiment)}
</span>

</div>

<h3 style="margin-top:12px">

<a href="${esc(n.link)}"
target="_blank" rel="noopener">

${esc(n.title)}

</a>

</h3>

${n.summary ? `<p>${esc(n.summary)}</p>` : ""}

<div class="foot">

${n.published ?
new Date(n.published).toLocaleString() :
"Time unavailable"}

·

<a href="${esc(n.link)}"
target="_blank" rel="noopener"
style="color:#a78bfa">
Read full story ↗
</a>

</div>

</article>

`).join("") :
'<div class="card">News feeds temporarily unavailable. Try again shortly.</div>';

}


// UPDATE DASHBOARD

async function update() {

try {

let response = await fetch(
"/api/data",
{cache:"no-store"}
);

if(!response.ok)
throw Error(response.status);

D = await response.json();

document.getElementById("pc").textContent =
D.pools.length;

document.getElementById("hc").textContent =
D.pools.filter(x => x.risk >= 60).length;

document.getElementById("gc").textContent =
D.pools.filter(x => x.score >= 70).length;

document.getElementById("nc").textContent =
D.news.length;

document.getElementById("ps").textContent =
D.pool_error ||
"Updated: " +
(D.pool_updated ?
new Date(D.pool_updated).toLocaleTimeString() :
"waiting");

document.getElementById("ns").textContent =
D.news_error ||
"Updated: " +
(D.news_updated ?
new Date(D.news_updated).toLocaleTimeString() :
"waiting");

renderPools();
renderNews();

} catch(error) {

document.getElementById("ps").textContent =
"Dashboard update failed: " + error.message;

}

}


// FILTERS

document.querySelectorAll("#filters button")
.forEach(button => {

button.onclick = () => {

document.querySelectorAll("#filters button")
.forEach(b => b.classList.remove("active"));

button.classList.add("active");

filter = button.dataset.f;

renderPools();

};

});

document.getElementById("sort").onchange = renderPools;

update();

setInterval(update,30000);

</script>

</body>
</html>'''


# ==========================================
# WEB SERVER
# ==========================================

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/api/data":

            with lock:
                body = json.dumps(data).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header("Cache-Control","no-store")

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()
            self.wfile.write(body)

            return

        if self.path in ("/", "/index.html"):

            body = PAGE.encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()
            self.wfile.write(body)

            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        print("[CoinSentraAI]", fmt % args)


# ==========================================
# START COINSENTRA AI
# ==========================================

if __name__ == "__main__":

    threading.Thread(
        target=pool_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=news_loop,
        daemon=True
    ).start()

    print(
        f"CoinSentra AI 5.0 starting on {HOST}:{PORT}"
    )

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    server.serve_forever()
