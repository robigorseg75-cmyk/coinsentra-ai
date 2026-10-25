import app
import json
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HOST = "127.0.0.1"
PORT = 8080

API_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/new_pools"
)

REFRESH_SECONDS = 30

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport"
 content="width=device-width, initial-scale=1.0">

<title>CoinSentra AI</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #080a12;
    color: #eeeeff;
    font-family: Arial, sans-serif;
}

header {
    padding: 22px 16px;
    background: #101020;
    border-bottom: 1px solid #28234c;
}

.logo {
    font-size: 27px;
    font-weight: bold;
    color: #a78bfa;
}

.subtitle {
    color: #999ab8;
    font-size: 13px;
    margin-top: 6px;
}

.status {
    margin-top: 12px;
    color: #72f5b0;
    font-size: 12px;
}

main {
    padding: 15px;
    max-width: 1100px;
    margin: auto;
}

.stats {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 18px;
}

.stat {
    background: #111323;
    border: 1px solid #292944;
    border-radius: 12px;
    padding: 15px 12px;
}

.stat-label {
    font-size: 12px;
    color: #a6a7c5;
    margin-bottom: 9px;
}

.stat-value {
    font-size: 25px;
    font-weight: bold;
    color: #b6a0ff;
}

.toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
    margin-bottom: 16px;
}

button, select {
    background: #17172a;
    color: #eeeeff;
    border: 1px solid #45406c;
    border-radius: 8px;
    padding: 11px;
    font-size: 13px;
}

button {
    cursor: pointer;
}

button:active {
    background: #302454;
}

.notice {
    background: #151426;
    border: 1px solid #383052;
    padding: 12px;
    border-radius: 10px;
    color: #c5c4dc;
    font-size: 12px;
    line-height: 1.6;
    margin-bottom: 16px;
}

#message {
    color: #aaa9c7;
    margin: 12px 0;
    font-size: 13px;
}

.token {
    background: #111323;
    border: 1px solid #292944;
    border-radius: 12px;
    padding: 15px;
    margin-bottom: 12px;
    overflow-wrap: anywhere;
}

.token-top {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: flex-start;
}

.token-name {
    font-size: 17px;
    font-weight: bold;
}

.token-symbol {
    color: #aaa9c7;
    font-size: 12px;
    margin-top: 5px;
}

.score {
    font-size: 23px;
    font-weight: bold;
    color: #73f4ae;
    text-align: right;
}

.score-label {
    font-size: 10px;
    color: #aaa9c7;
    text-align: right;
    margin-top: 4px;
}

.badge {
    display: inline-block;
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: bold;
    margin-top: 10px;
}

.buy {
    background: #123c2d;
    color: #73f4ae;
}

.watch {
    background: #38301a;
    color: #ffd56b;
}

.risk {
    background: #461e2a;
    color: #ff899f;
}

.metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin-top: 15px;
}

.metric {
    background: #0b0d18;
    padding: 10px;
    border-radius: 7px;
}

.metric-label {
    color: #999ab8;
    font-size: 11px;
    margin-bottom: 6px;
}

.metric-value {
    font-size: 14px;
    font-weight: bold;
}

.address {
    color: #a78bfa;
    font-size: 11px;
    margin-top: 13px;
    word-break: break-all;
}

.links {
    margin-top: 12px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

.links a {
    color: #b9a6ff;
    font-size: 12px;
    text-decoration: none;
}

.empty {
    text-align: center;
    padding: 35px 12px;
    color: #aaa9c7;
    background: #111323;
    border-radius: 12px;
}

footer {
    padding: 22px 15px;
    color: #777892;
    font-size: 11px;
    line-height: 1.7;
    text-align: center;
}

@media (min-width: 700px) {
    .stats {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }

    .metrics {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }
}
</style>
</head>

<body>

<header>
    <div class="logo">CoinSentra AI</div>
    <div class="subtitle">
        Solana New Token Scanner
    </div>
    <div class="status" id="status">
        ● INITIALIZING SCANNER
    </div>
</header>

<main>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">Pools Found</div>
            <div class="stat-value" id="total">0</div>
        </div>

        <div class="stat">
            <div class="stat-label">High-Risk Tokens</div>
            <div class="stat-value" id="high">0</div>
        </div>

        <div class="stat">
            <div class="stat-label">Opportunity Score 70+</div>
            <div class="stat-value" id="opportunities">0</div>
        </div>

        <div class="stat">
            <div class="stat-label">Last Update</div>
            <div class="stat-value" id="updated"
                 style="font-size:17px">--:--</div>
        </div>
    </div>

    <div class="toolbar">
        <button onclick="loadPools()">Refresh Now</button>

        <select id="filter" onchange="renderPools()">
            <option value="all">All Tokens</option>
            <option value="opportunities">Score 70+</option>
            <option value="risk">High Risk</option>
            <option value="liquid">Liquidity $1,000+</option>
        </select>

        <select id="sort" onchange="renderPools()">
            <option value="score">Sort: Opportunity Score</option>
            <option value="liquidity">Sort: Liquidity</option>
            <option value="volume">Sort: Volume</option>
            <option value="age">Sort: Newest First</option>
        </select>
    </div>

    <div class="notice">
        <strong>Important:</strong>
        Opportunity Score is a heuristic indicator based on
        available pool data. It is not a prediction or a guarantee.
        A high score does not mean a token is safe or profitable.
    </div>

    <div id="message">Connecting to market data...</div>

    <section id="tokens"></section>

</main>

<footer>
    CoinSentra AI 4.0<br>
    Data provider: GeckoTerminal public API<br>
    Market data may be delayed or incomplete.
    Always verify token contracts before trading.
</footer>

<script>
let pools = [];
let loading = false;

function number(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

function money(value) {
    value = number(value);

    if (value >= 1000000000) {
        return "$" + (value / 1000000000).toFixed(2) + "B";
    }

    if (value >= 1000000) {
        return "$" + (value / 1000000).toFixed(2) + "M";
    }

    if (value >= 1000) {
        return "$" + (value / 1000).toFixed(2) + "K";
    }

    return "$" + value.toFixed(value < 1 ? 6 : 2);
}

function ageText(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) {
        return "Unknown";
    }

    if (seconds < 60) {
        return Math.floor(seconds) + " sec";
    }

    if (seconds < 3600) {
        return Math.floor(seconds / 60) + " min";
    }

    if (seconds < 86400) {
        return Math.floor(seconds / 3600) + " hr";
    }

    return Math.floor(seconds / 86400) + " days";
}

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, function(ch) {
        const map = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
        };
        return map[ch];
    });
}

async function loadPools() {
    if (loading) return;

    loading = true;

    document.getElementById("status").textContent =
        "● FETCHING MARKET DATA";

    document.getElementById("message").textContent =
        "Loading the latest Solana pools...";

    try {
        const response = await fetch("/api/pools", {
            cache: "no-store"
        });

        const result = await response.json();

        if (!response.ok || !result.ok) {
            throw new Error(result.error || "Market data unavailable");
        }

        pools = result.pools || [];

        document.getElementById("total").textContent =
            pools.length;

        document.getElementById("high").textContent =
            pools.filter(p => p.riskScore >= 60).length;

        document.getElementById("opportunities").textContent =
            pools.filter(p => p.score >= 70 && p.riskScore < 60).length;

        document.getElementById("updated").textContent =
            new Date().toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit"
            });

        document.getElementById("status").textContent =
            "● LIVE DATA CONNECTION";

        document.getElementById("message").textContent =
            pools.length
            ? "Showing " + pools.length + " pools. Auto-refresh: 30 seconds."
            : "No pools returned. Try refreshing in a moment.";

        renderPools();

    } catch (error) {
        document.getElementById("status").textContent =
            "● DATA CONNECTION ERROR";

        document.getElementById("message").textContent =
            "Error: " + error.message +
            ". Check your internet connection and try again.";

    } finally {
        loading = false;
    }
}

function renderPools() {
    const container = document.getElementById("tokens");
    const filter = document.getElementById("filter").value;
    const sort = document.getElementById("sort").value;

    let list = pools.slice();

    if (filter === "opportunities") {
        list = list.filter(p => p.score >= 70 && p.riskScore < 60);
    }

    if (filter === "risk") {
        list = list.filter(p => p.riskScore >= 60);
    }

    if (filter === "liquid") {
        list = list.filter(p => p.liquidity >= 1000);
    }

    if (sort === "score") {
        list.sort((a, b) => b.score - a.score);
    }

    if (sort === "liquidity") {
        list.sort((a, b) => b.liquidity - a.liquidity);
    }

    if (sort === "volume") {
        list.sort((a, b) => b.volume - a.volume);
    }

    if (sort === "age") {
        list.sort((a, b) => a.ageSeconds - b.ageSeconds);
    }

    if (!list.length) {
        container.innerHTML =
            '<div class="empty">No matching tokens found.</div>';
        return;
    }

    container.innerHTML = list.map(p => {
        let badge = "WATCH";
        let badgeClass = "watch";

        if (p.riskScore >= 60) {
            badge = "HIGH RISK";
            badgeClass = "risk";
        } else if (p.score >= 70) {
            badge = "BUY WATCH";
            badgeClass = "buy";
        }

        const tokenAddress = encodeURIComponent(p.tokenAddress || "");
        const poolAddress = encodeURIComponent(p.address || "");

        const dexLink = "https://dexscreener.com/solana/" + poolAddress;
        const explorerLink = "https://solscan.io/token/" + tokenAddress;

        return `
        <article class="token">
            <div class="token-top">
                <div>
                    <div class="token-name">${escapeHTML(p.name)}</div>
                    <div class="token-symbol">${escapeHTML(p.symbol)}</div>
                    <span class="badge ${badgeClass}">${badge}</span>
                </div>

                <div>
                    <div class="score">${p.score}/100</div>
                    <div class="score-label">OPPORTUNITY SCORE</div>
                </div>
            </div>

            <div class="metrics">
                <div class="metric">
                    <div class="metric-label">Price</div>
                    <div class="metric-value">${money(p.price)}</div>
                </div>

                <div class="metric">
                    <div class="metric-label">Liquidity</div>
                    <div class="metric-value">${money(p.liquidity)}</div>
                </div>

                <div class="metric">
                    <div class="metric-label">24h Volume</div>
                    <div class="metric-value">${money(p.volume)}</div>
                </div>

                <div class="metric">
                    <div class="metric-label">Market Cap / FDV</div>
                    <div class="metric-value">${money(p.marketCap)}</div>
                </div>

                <div class="metric">
                    <div class="metric-label">Pool Age</div>
                    <div class="metric-value">${ageText(p.ageSeconds)}</div>
                </div>

                <div class="metric">
                    <div class="metric-label">Risk Score</div>
                    <div class="metric-value">${p.riskScore}/100</div>
                </div>
            </div>

            <div class="address">
                Pool: ${escapeHTML(p.address)}
            </div>

            <div class="links">
                <a href="${dexLink}" target="_blank" rel="noopener">
                    View on DexScreener
                </a>

                <a href="${explorerLink}" target="_blank" rel="noopener">
                    View on Solscan
                </a>
            </div>
        </article>`;
    }).join("");
}

loadPools();
setInterval(loadPools, 30000);
</script>

</body>
</html>
"""


def safe_number(value):
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return result
    except (TypeError, ValueError):
        return 0.0


def get_age_seconds(created_at):
    if not created_at:
        return 999999999

    try:
        created = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        )

        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        return max(0, int((now - created).total_seconds()))

    except (ValueError, TypeError):
        return 999999999


def calculate_scores(liquidity, volume, age_seconds):
    score = 0
    risk = 0

    # Liquidity score
    if liquidity >= 50000:
        score += 30
    elif liquidity >= 20000:
        score += 25
    elif liquidity >= 10000:
        score += 20
    elif liquidity >= 5000:
        score += 15
    elif liquidity >= 1000:
        score += 8
    else:
        risk += 35

    # Volume score
    if volume >= 100000:
        score += 25
    elif volume >= 25000:
        score += 20
    elif volume >= 5000:
        score += 15
    elif volume >= 1000:
        score += 8

    # Age score
    if age_seconds <= 300:
        score += 20
        risk += 25
    elif age_seconds <= 1800:
        score += 18
        risk += 15
    elif age_seconds <= 7200:
        score += 12
        risk += 8
    elif age_seconds <= 86400:
        score += 8
    else:
        score += 4

    # Low liquidity relative to volume can be unstable.
    if liquidity > 0 and volume > liquidity * 10:
        risk += 15

    score = min(100, max(0, score))
    risk = min(100, max(0, risk))

    return score, risk


def fetch_pools():
    request = Request(
        API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "CoinSentraAI/4.0"
        }
    )

    with urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8")

    payload = json.loads(raw)

    data = payload.get("data", [])
    included = payload.get("included", [])

    token_map = {}

    for item in included:
        if item.get("type") == "token":
            token_map[item.get("id", "")] = item.get("attributes", {})

    results = []

    for pool in data:
        attributes = pool.get("attributes", {})
        relationships = pool.get("relationships", {})

        base_data = relationships.get("base_token", {}).get("data", {})
        base_id = base_data.get("id", "")
        token = token_map.get(base_id, {})

        pool_address = attributes.get("address", "")
        token_address = token.get("address", "")

        pool_name = attributes.get("name", "Unknown Pool")
        token_name = token.get("name", "")
        token_symbol = token.get("symbol", "")

        if not token_name:
            token_name = pool_name.split(" / ")[0].strip()

        if not token_symbol:
            token_symbol = token_name[:12]

        liquidity = safe_number(attributes.get("reserve_in_usd"))

        volume_data = attributes.get("volume_usd", {})
        if not isinstance(volume_data, dict):
            volume_data = {}

        volume = safe_number(
            volume_data.get("h24", 0)
        )

        price = safe_number(
            attributes.get("base_token_price_usd")
        )

        market_cap = safe_number(
            attributes.get("market_cap_usd")
        )

        if market_cap <= 0:
            market_cap = safe_number(
                attributes.get("fdv_usd")
            )

        created_at = attributes.get("pool_created_at")
        age_seconds = get_age_seconds(created_at)

        score, risk = calculate_scores(
            liquidity,
            volume,
            age_seconds
        )

        results.append({
            "name": str(token_name)[:100],
            "symbol": str(token_symbol)[:30],
            "address": str(pool_address),
            "tokenAddress": str(token_address),
            "price": price,
            "liquidity": liquidity,
            "volume": volume,
            "marketCap": market_cap,
            "ageSeconds": age_seconds,
            "score": score,
            "riskScore": risk
        })

    return results


class Handler(BaseHTTPRequestHandler):

    def send_content(self, status, content, content_type):
        encoded = content.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.send_content(
                200,
                HTML,
                "text/html; charset=utf-8"
            )
            return

        if self.path.startswith("/api/pools"):
            try:
                pools = fetch_pools()

                response = {
                    "ok": True,
                    "pools": pools,
                    "count": len(pools),
                    "updated": datetime.now(
                        timezone.utc
                    ).isoformat()
                }

                self.send_content(
                    200,
                    json.dumps(response),
                    "application/json; charset=utf-8"
                )

            except HTTPError as error:
                response = {
                    "ok": False,
                    "error": "Market API returned HTTP "
                              + str(error.code)
                }

                self.send_content(
                    502,
                    json.dumps(response),
                    "application/json; charset=utf-8"
                )

            except (URLError, TimeoutError, OSError) as error:
                response = {
                    "ok": False,
                    "error": "Network connection failed: "
                              + str(error)
                }

                self.send_content(
                    502,
                    json.dumps(response),
                    "application/json; charset=utf-8"
                )

            except (ValueError, TypeError, KeyError) as error:
                response = {
                    "ok": False,
                    "error": "Could not process market data: "
                              + str(error)
                }

                self.send_content(
                    502,
                    json.dumps(response),
                    "application/json; charset=utf-8"
                )

            except Exception as error:
                response = {
                    "ok": False,
                    "error": "Unexpected error: " + str(error)
                }

                self.send_content(
                    500,
                    json.dumps(response),
                    "application/json; charset=utf-8"
                )

            return

        self.send_content(404, "Not found", "text/plain")

    def log_message(self, format_string, *args):
        return


def main():
    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    print("")
    print("===================================")
    print("       CoinSentra AI 4.0")
    print("===================================")
    print("Server running at:")
    print("http://127.0.0.1:8080")
    print("")
    print("Keep Python running.")
    print("Open the address above in Chrome.")
    print("Press Ctrl+C to stop the server.")
    print("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCoinSentra AI stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
