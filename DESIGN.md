# NSE Real-Time Market Data Capture System â€” Design Document

> **Version:** 2.1  
> **Date:** 2026-02-18  
> **Status:** ACTIVE â€” Binary parser fix deployed, clean data ingestion running  

---

## 1. Executive Summary

This system captures **real-time NSE NIFTY 50 Weekly Options data** via the **IIFL Capital Markets API** WebSocket feed, stores it in a **TimescaleDB** container on the existing **YOUR_VM_NAME** Azure VM, and exposes the data through a REST API + Server-Sent Events (SSE) endpoint for **real-time zero-delay feeds** to Microsoft 365 Excel, Google Sheets, and other applications.

### Phased Rollout
| Phase | Scope | Priority |
|-------|-------|----------|
| **Phase 1** | NIFTY 50 Weekly Options (full option chain) | ðŸ”´ Now |
| **Phase 2** | BANKNIFTY Weekly Options | ðŸŸ¡ Next |
| **Phase 3** | NIFTY & BANKNIFTY Futures | ðŸŸ¢ Later |
| **Phase 4** | NIFTY 50 Constituent Stocks (Equity) | ðŸ”µ Final |

---

## 2. Existing Infrastructure (DO NOT MODIFY)

### Azure VM: YOUR_VM_NAME
| Property | Value |
|----------|-------|
| Resource Group | `YOUR_RESOURCE_GROUP` |
| VM Name | `YOUR_VM_NAME` |
| Public IP | `YOUR_VM_IP` |
| Location | South India |
| SSH User | `azureuser` |
| SSH Key | `C:\Users\Tharun\.ssh\id_rsa` (local) / `~/.ssh/id_rsa` (default) |
| Domain | `YOUR_DOMAIN` (Hostinger) |

### Running Containers (âš ï¸ DO NOT TOUCH)
| Container | Image | Ports | Purpose |
|-----------|-------|-------|---------|
| `evolution_api` | evoapicloud/evolution-api:v2.3.7 | 8080 | WhatsApp Gateway |
| `postgres` | postgres:15-alpine | 5432 (internal) | Evolution DB |
| `redis` | redis:7-alpine | 6379 (internal) | Evolution Cache |
| `minio` | minio/minio:latest | 9000, 9001 | S3 Storage |
| `telegram_bridge` | custom build | 5000 (internal) | Telegram Bot |
| `n8n` | n8nio/n8n:latest | 5678 (internal) | Workflow Automation |
| `nginx` | nginx:alpine | **80, 443** | Reverse Proxy + SSL |
| `certbot` | certbot/certbot:latest | â€” | SSL Cert Management |

### Existing Docker Network
- Network: `app_network` (bridge)
- All existing containers are on this network

### Existing Nginx Server Blocks
| Domain | Backend |
|--------|---------|
| `whatsapp.YOUR_DOMAIN` | `evolution_api:8080` |
| `minio.YOUR_DOMAIN` | `minio:9000` |
| `n8n.YOUR_DOMAIN` | `n8n:5678` |

### Existing Certbot SSL Certificates
| Domain | Path |
|--------|------|
| `whatsapp.YOUR_DOMAIN` | `/etc/letsencrypt/live/whatsapp.YOUR_DOMAIN/` |
| `minio.YOUR_DOMAIN` | `/etc/letsencrypt/live/minio.YOUR_DOMAIN/` |
| `n8n.YOUR_DOMAIN` | `/etc/letsencrypt/live/n8n.YOUR_DOMAIN/` |

---

## 3. Integration Strategy with Existing Setup

### What We REUSE âœ…
| Component | How We Use It |
|-----------|---------------|
| **Nginx container** | Add a NEW server block (`nsedata.YOUR_DOMAIN`) â€” no changes to existing configs |
| **Certbot container** | Obtain a NEW SSL certificate for `nsedata.YOUR_DOMAIN` â€” no changes to existing certs |
| **Docker network** (`app_network`) | Join all new containers to the same network |

### What We ADD (New Containers) ðŸ†•
| Container | Image | Purpose |
|-----------|-------|---------|
| `nse-timescaledb` | timescale/timescaledb:latest-pg16 | Time-series DB (separate from existing postgres!) |
| `nse-data-ingestor` | Custom Python | IIFL WebSocket â†’ TimescaleDB |
| `nse-data-api` | Custom FastAPI | REST + SSE API for consumers |

### What We DO NOT TOUCH âŒ
- Existing `postgres` container (port 5432) â€” we use a **separate TimescaleDB** on port **5433**
- Existing `redis` container
- Existing nginx config files (`default.conf`, `minio.conf`, `n8n.conf`)
- Any existing Docker volumes
- Existing Evolution API, n8n, MinIO, Telegram bridge

### Why Separate TimescaleDB (Not Reusing Existing PostgreSQL)?
1. **Different PostgreSQL version** â€” Existing is 15-alpine; TimescaleDB needs pg16 with extensions
2. **Workload isolation** â€” Tick data ingestion (thousands of writes/sec) must not impact Evolution/n8n
3. **Independent backup/retention** â€” Time-series data has different lifecycle than app data
4. **No risk** â€” Zero chance of affecting existing services

---

## 4. Architecture Overview

```
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚                    AZURE VM: YOUR_VM_NAME (YOUR_VM_IP)                      â”‚
â”‚                                                                              â”‚
â”‚  â”Œâ”€â”€â”€ EXISTING (DO NOT TOUCH) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”    â”‚
â”‚  â”‚  evolution_api Â· postgres Â· redis Â· minio Â· n8n Â· telegram_bridge   â”‚    â”‚
â”‚  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜    â”‚
â”‚                                                                              â”‚
â”‚  â”Œâ”€â”€â”€ NEW: NSE Data Stack â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”    â”‚
â”‚  â”‚                                                                      â”‚    â”‚
â”‚  â”‚  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”    â”‚    â”‚
â”‚  â”‚  â”‚ TimescaleDB   â”‚  â”‚  Data Ingestor   â”‚  â”‚  Data API (FastAPI)â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ (PG16 + ext)  â”‚â—„â”€â”‚  (Python async)  â”‚  â”‚  REST + SSE        â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ Port: 5433    â”‚  â”‚                  â”‚  â”‚  Port: 8088        â”‚    â”‚    â”‚
â”‚  â”‚  â”‚               â”‚  â”‚  IIFL WebSocket  â”‚  â”‚                    â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ â€¢ tick_data   â”‚  â”‚  Binary Parser   â”‚  â”‚  /api/v1/quotes    â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ â€¢ ohlcv       â”‚  â”‚  Batch Writer    â”‚  â”‚  /api/v1/chain     â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ â€¢ instruments â”‚  â”‚  Auth Manager    â”‚  â”‚  /api/v1/stream    â”‚    â”‚    â”‚
â”‚  â”‚  â”‚ â€¢ oi_data     â”‚  â”‚                  â”‚  â”‚  (SSE real-time)   â”‚    â”‚    â”‚
â”‚  â”‚  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜  â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜    â”‚    â”‚
â”‚  â”‚                                                     â”‚               â”‚    â”‚
â”‚  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜    â”‚
â”‚                                                        â”‚                    â”‚
â”‚  â”Œâ”€â”€â”€ SHARED (ADD NEW CONFIG ONLY) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”     â”‚                    â”‚
â”‚  â”‚  nginx (existing container)                    â”‚â—„â”€â”€â”€â”€â”˜                    â”‚
â”‚  â”‚  + nsedata.conf (NEW server block)            â”‚                          â”‚
â”‚  â”‚  certbot (existing container)                  â”‚                          â”‚
â”‚  â”‚  + SSL for nsedata.YOUR_DOMAIN (NEW cert)     â”‚                          â”‚
â”‚  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜                          â”‚
â”‚                                                                              â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                              â”‚
           â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
           â”‚                  â”‚                  â”‚
   â”Œâ”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”  â”Œâ”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”  â”Œâ”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”
   â”‚ O365 Excel    â”‚  â”‚Google Sheets â”‚  â”‚ Custom Apps  â”‚
   â”‚ (Power Query  â”‚  â”‚(Apps Script) â”‚  â”‚ (WebSocket/  â”‚
   â”‚  + SSE)       â”‚  â”‚             â”‚  â”‚  REST/SSE)   â”‚
   â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
```

---

## 5. IIFL Capital Markets API Integration

| Property | Value |
|---|---|
| **API Provider** | IIFL Capital Markets (https://developers.iiflcapital.com/apidocs/introduction) |
| **NOT Using** | âŒ IIFL Blaze, âŒ IIFL XTS |
| **Base URL** | `https://api.iiflcapital.com/v1` |
| **WebSocket** | `bridge.iiflcapital.com:8883` |
| **Auth Method** | OAuth-style: Login â†’ authCode + clientId â†’ `POST /getusersession` â†’ Bearer Token |
| **Checksum** | `SHA256(clientId + authCode + apiSecret)` |
| **Token Validity** | Once per trading day |

### Credentials (from `config.json`)
| Key | Value |
|-----|-------|
| `api_key` (appKey) | `YOUR_APP_KEY` |
| `api_secret` | `psZqMp8kME...` (stored in config.json) |
| `client_id` | `YOUR_CLIENT_ID` |
| `auth_code` | `YOUR_DAILY_AUTH_CODE` (refreshed daily) |

### Authentication Flow
```
 User Browser                    IIFL                        Our System
      â”‚                           â”‚                              â”‚
      â”‚â”€â”€â”€â”€ Login redirect â”€â”€â”€â”€â”€â”€â–ºâ”‚                              â”‚
      â”‚     (appKey + redirectUrl) â”‚                              â”‚
      â”‚â—„â”€â”€â”€ Redirect with â”€â”€â”€â”€â”€â”€â”€â”€â”‚                              â”‚
      â”‚     authCode + clientId    â”‚                              â”‚
      â”‚                           â”‚                              â”‚
      â”‚â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”‚â”€â”€â–º POST /getusersession â”€â”€â”€â–ºâ”‚
      â”‚                           â”‚    (clientId, authCode,      â”‚
      â”‚                           â”‚     checkSum=SHA256(...))    â”‚
      â”‚                           â”‚â—„â”€â”€ userSession (Bearer)  â—„â”€â”€â”‚
```

> **Automated Login:** Uses Playwright headless browser to obtain `authCode` daily at 8:45 AM IST.

### WebSocket Market Feed
- **Protocol:** Binary (188-byte packets per instrument)
- **Subscription:** `{"subscriptionList": ["nsefo/35005", ...]}`
- **Binary packet fields:** LTP, LTQ, Volume, OHLC, Best Bid/Ask, 5-level Depth

### REST Endpoints Used
| Endpoint | Purpose | Rate Limit |
|---|---|---|
| `POST /getusersession` | Daily auth | â€” |
| `POST /marketdata/marketquotes` | Snapshot quotes | 10 req/s |
| `POST /marketdata/marketdepth` | Level-2 depth | 10 req/s |
| `POST /marketdata/historicaldata` | OHLCV candles (backfill) | 2 req/s |
| `POST /marketdata/openinterest` | F&O OI data | 10 req/s |
| `GET /contractfiles/NSEFO.json` | F&O instrument master | Daily |
| `GET /contractfiles/NSEEQ.json` | Equity instrument master | Daily |

---

## 6. Phase 1: NIFTY 50 Weekly Options

### What We Capture
- **Underlying:** NIFTY 50 Index
- **Instrument Type:** Weekly Options (CE + PE)
- **Expiry:** Current week + next week (rolling)
- **Strikes:** ATM Â± 20 strikes (configurable)
- **Data Points:** LTP, OI, Change in OI, Volume, Bid/Ask, Greeks (calculated)

### Instrument Discovery
Each trading day at 8:45 AM IST:
1. Download `NSEFO.json` from IIFL
2. Filter for: `instrumentType = OPTIDX`, `symbol = NIFTY`, `expiry = current_week OR next_week`
3. From the ATM price (previous day close), select strikes: ATM Â± 20 Ã— step_size
4. Build subscription list: `["nsefo/{instrumentId}", ...]`
5. Subscribe via WebSocket

### Estimated Subscriptions (Phase 1)
- ~40 strikes Ã— 2 (CE+PE) Ã— 2 expiries = **~160 instruments**
- Well within IIFL's 4,000 instrument limit

---

## 7. Database Schema (TimescaleDB)

```sql
-- Extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===== INSTRUMENTS TABLE =====
CREATE TABLE instruments (
    instrument_id   BIGINT PRIMARY KEY,
    exchange        VARCHAR(10) NOT NULL,     -- NSE, BSE, MCX
    segment         VARCHAR(10) NOT NULL,     -- EQ, FO, CURR
    symbol          VARCHAR(50) NOT NULL,     -- NIFTY, BANKNIFTY, RELIANCE
    name            VARCHAR(200),
    instrument_type VARCHAR(20),              -- OPTIDX, OPTSTK, FUTIDX, FUTSTK, EQUITY
    expiry_date     DATE,
    strike_price    DECIMAL(15,2),
    option_type     VARCHAR(5),               -- CE, PE, null for futures/equity
    lot_size        INT,
    tick_size       DECIMAL(10,4),
    is_active       BOOLEAN DEFAULT true,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_instruments_symbol ON instruments(symbol);
CREATE INDEX idx_instruments_type_expiry ON instruments(instrument_type, expiry_date);
CREATE INDEX idx_instruments_active ON instruments(is_active) WHERE is_active = true;

-- ===== TICK DATA (Hypertable) =====
CREATE TABLE tick_data (
    time                TIMESTAMPTZ NOT NULL,
    instrument_id       BIGINT NOT NULL,
    ltp                 DECIMAL(15,2),
    ltq                 BIGINT,
    total_traded_volume BIGINT,
    open                DECIMAL(15,2),
    high                DECIMAL(15,2),
    low                 DECIMAL(15,2),
    close               DECIMAL(15,2),
    best_bid_price      DECIMAL(15,2),
    best_bid_qty        BIGINT,
    best_ask_price      DECIMAL(15,2),
    best_ask_qty        BIGINT,
    bid_price_1 DECIMAL(15,2), bid_qty_1 BIGINT,
    bid_price_2 DECIMAL(15,2), bid_qty_2 BIGINT,
    bid_price_3 DECIMAL(15,2), bid_qty_3 BIGINT,
    bid_price_4 DECIMAL(15,2), bid_qty_4 BIGINT,
    bid_price_5 DECIMAL(15,2), bid_qty_5 BIGINT,
    ask_price_1 DECIMAL(15,2), ask_qty_1 BIGINT,
    ask_price_2 DECIMAL(15,2), ask_qty_2 BIGINT,
    ask_price_3 DECIMAL(15,2), ask_qty_3 BIGINT,
    ask_price_4 DECIMAL(15,2), ask_qty_4 BIGINT,
    ask_price_5 DECIMAL(15,2), ask_qty_5 BIGINT
);
SELECT create_hypertable('tick_data', 'time');
CREATE INDEX idx_tick_instrument ON tick_data(instrument_id, time DESC);

-- ===== OPEN INTEREST TABLE (Hypertable) =====
CREATE TABLE oi_data (
    time            TIMESTAMPTZ NOT NULL,
    instrument_id   BIGINT NOT NULL,
    open_interest   BIGINT,
    change_in_oi    BIGINT,
    ltp             DECIMAL(15,2),
    volume          BIGINT
);
SELECT create_hypertable('oi_data', 'time');
CREATE INDEX idx_oi_instrument ON oi_data(instrument_id, time DESC);

-- ===== OHLCV CANDLES (Continuous Aggregate) =====
CREATE MATERIALIZED VIEW ohlcv_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    instrument_id,
    first(ltp, time)  AS open,
    max(ltp)           AS high,
    min(ltp)           AS low,
    last(ltp, time)    AS close,
    last(total_traded_volume, time) AS volume
FROM tick_data
GROUP BY bucket, instrument_id;

-- ===== OPTION CHAIN SNAPSHOT VIEW =====
CREATE VIEW live_option_chain AS
SELECT DISTINCT ON (i.strike_price, i.option_type)
    i.symbol,
    i.strike_price,
    i.option_type,
    i.expiry_date,
    i.lot_size,
    t.ltp,
    t.total_traded_volume AS volume,
    t.best_bid_price AS bid,
    t.best_ask_price AS ask,
    o.open_interest,
    o.change_in_oi,
    t.time AS last_updated
FROM instruments i
LEFT JOIN LATERAL (
    SELECT * FROM tick_data td 
    WHERE td.instrument_id = i.instrument_id 
    ORDER BY td.time DESC LIMIT 1
) t ON true
LEFT JOIN LATERAL (
    SELECT * FROM oi_data od 
    WHERE od.instrument_id = i.instrument_id 
    ORDER BY od.time DESC LIMIT 1
) o ON true
WHERE i.is_active = true
  AND i.instrument_type IN ('OPTIDX', 'OPTSTK')
ORDER BY i.strike_price, i.option_type, t.time DESC;

-- ===== HISTORICAL BACKFILL TABLE =====
CREATE TABLE historical_ohlcv (
    time            TIMESTAMPTZ NOT NULL,
    instrument_id   BIGINT NOT NULL,
    interval        VARCHAR(10) NOT NULL,  -- 1m, 5m, 15m, 1d
    open            DECIMAL(15,2),
    high            DECIMAL(15,2),
    low             DECIMAL(15,2),
    close           DECIMAL(15,2),
    volume          BIGINT,
    oi              BIGINT
);
SELECT create_hypertable('historical_ohlcv', 'time');
CREATE INDEX idx_historical_instrument ON historical_ohlcv(instrument_id, interval, time DESC);

-- ===== COMPRESSION POLICIES =====
ALTER TABLE tick_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('tick_data', INTERVAL '7 days');

ALTER TABLE oi_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('oi_data', INTERVAL '30 days');

ALTER TABLE historical_ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('historical_ohlcv', INTERVAL '30 days');

-- ===== RETENTION =====
SELECT add_retention_policy('tick_data', INTERVAL '90 days');
-- historical_ohlcv retained indefinitely for backtesting
```

---

## 8. Data Ingestor Service

### Module Structure
```
data-ingestor/
â”œâ”€â”€ Dockerfile
â”œâ”€â”€ requirements.txt
â”œâ”€â”€ src/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ main.py                 # Entry point, orchestrator
â”‚   â”œâ”€â”€ config.py               # Env var config loader
â”‚   â”œâ”€â”€ auth/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â”œâ”€â”€ session_manager.py  # IIFL token exchange + renewal
â”‚   â”‚   â””â”€â”€ headless_login.py   # Playwright automated login
â”‚   â”œâ”€â”€ feed/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â”œâ”€â”€ ws_client.py        # WebSocket connection + reconnect
â”‚   â”‚   â”œâ”€â”€ binary_parser.py    # 188-byte packet decoder
â”‚   â”‚   â””â”€â”€ subscription.py     # Dynamic instrument subscription
â”‚   â”œâ”€â”€ db/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â”œâ”€â”€ connection.py       # asyncpg connection pool
â”‚   â”‚   â””â”€â”€ writer.py           # Buffered batch inserts
â”‚   â”œâ”€â”€ instruments/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â””â”€â”€ master.py           # Daily NSEFO.json download + ATM calc
â”‚   â”œâ”€â”€ backfill/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â””â”€â”€ historical.py       # Historical OHLCV backfill via REST
â”‚   â”œâ”€â”€ broadcast/
â”‚   â”‚   â”œâ”€â”€ __init__.py
â”‚   â”‚   â””â”€â”€ publisher.py        # In-memory pub/sub for SSE consumers
â”‚   â””â”€â”€ utils/
â”‚       â”œâ”€â”€ __init__.py
â”‚       â””â”€â”€ logger.py
â”œâ”€â”€ scripts/
â”‚   â””â”€â”€ init_db.sql
â””â”€â”€ tests/
```

### Key Design Decisions

1. **Batch Writes:** Ticks buffered (100 ticks or 500ms, whichever first) â†’ `asyncpg.copy_records_to_table()` for max throughput
2. **In-Memory Broadcast:** Each parsed tick is also pushed to an `asyncio.Queue` for real-time SSE consumers (zero DB latency for live clients)
3. **Reconnection:** Exponential backoff (1s â†’ 2s â†’ 4s â†’ max 60s) + auto re-subscribe
4. **Dynamic Subscription:** Strike range auto-adjusts as NIFTY moves (recalculates every 5 minutes)
5. **Historical Backfill:** On startup (if flag set), fetches OHLCV from IIFL REST API at 2 req/s, respecting rate limits
6. **Graceful Shutdown:** SIGTERM/SIGINT â†’ flush buffers â†’ close WebSocket â†’ exit

---

## 9. Data Distribution API (FastAPI + SSE)

### Module Structure
```
data-api/
â”œâ”€â”€ Dockerfile
â”œâ”€â”€ requirements.txt
â”œâ”€â”€ src/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ main.py              # FastAPI app
â”‚   â”œâ”€â”€ config.py
â”‚   â”œâ”€â”€ auth/
â”‚   â”‚   â””â”€â”€ api_key.py       # X-API-Key header validation
â”‚   â”œâ”€â”€ routes/
â”‚   â”‚   â”œâ”€â”€ quotes.py        # GET /api/v1/quotes/{symbol}
â”‚   â”‚   â”œâ”€â”€ ohlcv.py         # GET /api/v1/ohlcv/{symbol}
â”‚   â”‚   â”œâ”€â”€ option_chain.py  # GET /api/v1/chain/{symbol}
â”‚   â”‚   â”œâ”€â”€ oi.py            # GET /api/v1/oi/{symbol}
â”‚   â”‚   â”œâ”€â”€ instruments.py   # GET /api/v1/instruments
â”‚   â”‚   â”œâ”€â”€ stream.py        # GET /api/v1/stream (SSE real-time)
â”‚   â”‚   â”œâ”€â”€ historical.py    # GET /api/v1/historical/{symbol}
â”‚   â”‚   â””â”€â”€ health.py        # GET /health
â”‚   â”œâ”€â”€ db/
â”‚   â”‚   â””â”€â”€ connection.py
â”‚   â””â”€â”€ schemas/
â”‚       â””â”€â”€ responses.py     # Pydantic models
â””â”€â”€ tests/
```

### API Endpoints

| Method | Endpoint | Description | Real-Time? |
|---|---|---|---|
| `GET` | `/api/v1/quotes/{symbol}` | Latest quote | Snapshot |
| `GET` | `/api/v1/quotes/bulk?symbols=...` | Bulk quotes | Snapshot |
| `GET` | `/api/v1/chain/{symbol}?expiry=...` | Full option chain | Snapshot |
| `GET` | `/api/v1/oi/{symbol}` | Open interest data | Snapshot |
| `GET` | `/api/v1/ohlcv/{symbol}?interval=1m` | OHLCV candles | Snapshot |
| `GET` | `/api/v1/historical/{symbol}?from=...&to=...` | Historical backfill data | Snapshot |
| `GET` | `/api/v1/instruments?type=OPTIDX` | Instrument lookup | Snapshot |
| `GET` | `/api/v1/stream?symbols=...` | **SSE real-time stream** | âœ… Real-Time |
| `GET` | `/health` | Health check | â€” |

### Real-Time Delivery: Server-Sent Events (SSE)

**Why SSE over WebSocket for consumers?**
- SSE works natively with Excel Power Query (via streaming data types)
- SSE works with Google Apps Script `UrlFetchApp`
- Simpler than WebSocket for consumers â€” just HTTP GET with `text/event-stream`
- Auto-reconnect built into browsers/clients
- One-directional (server â†’ client) is exactly what we need

```
Client                                     Data API
  â”‚                                           â”‚
  â”‚â”€â”€ GET /api/v1/stream?symbols=NIFTY â”€â”€â”€â”€â”€â–ºâ”‚
  â”‚                                           â”‚
  â”‚â—„â”€â”€ HTTP 200, Content-Type: text/event-stream
  â”‚                                           â”‚
  â”‚â—„â”€â”€ data: {"symbol":"NIFTY","ltp":23145.50,"time":"..."}
  â”‚â—„â”€â”€ data: {"symbol":"NIFTY","ltp":23146.00,"time":"..."}
  â”‚â—„â”€â”€ data: {"symbol":"NIFTY","ltp":23144.75,"time":"..."}
  â”‚    ... (continuous, zero delay) ...        â”‚
```

### Response Format Example: `/api/v1/chain/NIFTY?expiry=2026-02-19`
```json
{
  "status": "success",
  "data": {
    "symbol": "NIFTY",
    "underlying_ltp": 23145.50,
    "expiry": "2026-02-19",
    "timestamp": "2026-02-12T15:29:45+05:30",
    "chain": [
      {
        "strike": 23000,
        "ce": {
          "ltp": 245.30, "oi": 1523400, "change_oi": 125600,
          "volume": 89234, "bid": 244.80, "ask": 245.50, "iv": 14.2
        },
        "pe": {
          "ltp": 98.70, "oi": 982100, "change_oi": -45200,
          "volume": 56789, "bid": 98.20, "ask": 99.00, "iv": 15.1
        }
      },
      ...
    ],
    "totals": {
      "total_ce_oi": 15234000, "total_pe_oi": 12340000,
      "pcr": 0.81, "max_pain": 23100
    }
  }
}
```

---

## 10. Docker Compose (NSE Stack Only)

This is a **separate** docker-compose file deployed to a separate directory on the VM. It joins the existing `app_network` so Nginx can reach the API.

```yaml
# /home/azureuser/nse-market-data/docker-compose.yml

services:
  nse-timescaledb:
    image: timescale/timescaledb:latest-pg16
    container_name: nse-timescaledb
    restart: unless-stopped
    ports:
      - "5433:5432"   # Port 5433 externally, avoids clash with existing postgres:5432
    environment:
      POSTGRES_USER: ${DB_USER:-nseadmin}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME:-nse_market_data}
    volumes:
      - nse_timescaledb_data:/var/lib/postgresql/data
      - ./scripts/init_db.sql:/docker-entrypoint-initdb.d/init_db.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-nseadmin}"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app_network

  nse-data-ingestor:
    build: ./data-ingestor
    container_name: nse-data-ingestor
    restart: unless-stopped
    depends_on:
      nse-timescaledb:
        condition: service_healthy
    environment:
      - DB_HOST=nse-timescaledb
      - DB_PORT=5432
      - DB_USER=${DB_USER:-nseadmin}
      - DB_PASSWORD=${DB_PASSWORD}
      - DB_NAME=${DB_NAME:-nse_market_data}
      - IIFL_APP_KEY=${IIFL_APP_KEY}
      - IIFL_API_SECRET=${IIFL_API_SECRET}
      - IIFL_CLIENT_ID=${IIFL_CLIENT_ID}
      - IIFL_AUTH_CODE=${IIFL_AUTH_CODE}
      - IIFL_REDIRECT_URL=${IIFL_REDIRECT_URL:-http://localhost:3000/callback}
      - SUBSCRIPTION_SYMBOL=NIFTY
      - SUBSCRIPTION_SEGMENT=NSEFO
      - STRIKE_RANGE=20
      - ENABLE_BACKFILL=${ENABLE_BACKFILL:-false}
      - BACKFILL_DAYS=${BACKFILL_DAYS:-30}
      - TZ=Asia/Kolkata
    networks:
      - app_network

  nse-data-api:
    build: ./data-api
    container_name: nse-data-api
    restart: unless-stopped
    ports:
      - "8088:8088"
    depends_on:
      nse-timescaledb:
        condition: service_healthy
    environment:
      - DB_HOST=nse-timescaledb
      - DB_PORT=5432
      - DB_USER=${DB_USER:-nseadmin}
      - DB_PASSWORD=${DB_PASSWORD}
      - DB_NAME=${DB_NAME:-nse_market_data}
      - API_KEY=${DATA_API_KEY}
      - API_PORT=8088
      - TZ=Asia/Kolkata
    networks:
      - app_network

networks:
  app_network:
    external: true   # Join the EXISTING network from the WhatsApp stack

volumes:
  nse_timescaledb_data:
    driver: local
```

### Key Isolation Details
| Aspect | Existing Stack | NSE Stack |
|--------|---------------|-----------|
| Compose file | `/home/azureuser/docker-compose.yml` | `/home/azureuser/nse-market-data/docker-compose.yml` |
| PostgreSQL | `postgres` on port 5432 | `nse-timescaledb` on port **5433** |
| API | `evolution_api` on port 8080 | `nse-data-api` on port **8088** |
| Network | `app_network` | `app_network` (shared, external) |
| Volumes | `postgres_data` | `nse_timescaledb_data` (separate) |

---

## 11. Nginx Configuration (New Server Block)

A **new file** added to the existing nginx conf directory. Existing configs are untouched.

```nginx
# /home/azureuser/nginx/conf.d/nsedata.conf (NEW FILE)

# HTTP -> HTTPS redirect
server {
    listen 80;
    server_name nsedata.YOUR_DOMAIN;
    server_tokens off;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# HTTPS server for nsedata.YOUR_DOMAIN
server {
    listen 443 ssl;
    http2 on;
    server_name nsedata.YOUR_DOMAIN;
    server_tokens off;

    ssl_certificate /etc/letsencrypt/live/nsedata.YOUR_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nsedata.YOUR_DOMAIN/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Security Headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # CORS headers for Excel/Sheets
    add_header Access-Control-Allow-Origin "*" always;
    add_header Access-Control-Allow-Methods "GET, OPTIONS" always;
    add_header Access-Control-Allow-Headers "X-API-Key, Content-Type" always;

    # Proxy to NSE Data API
    location / {
        proxy_pass http://nse-data-api:8088;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support (critical for real-time streaming)
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        chunked_transfer_encoding on;
    }
}
```

---

## 12. Consumer Integration

### Microsoft 365 Excel (Real-Time via Power Query + Auto-Refresh)

**Option A: Power Query (Polling every 1 second)**
```
= Json.Document(
    Web.Contents(
        "https://nsedata.YOUR_DOMAIN/api/v1/chain/NIFTY?expiry=2026-02-19",
        [Headers=[#"X-API-Key"="your-api-key"]]
    )
)
```
Set Background Refresh: every 1 second (minimum Excel supports)

**Option B: Excel with Office Scripts + SSE (True Real-Time)**
Use an Office Script that connects to SSE and updates cells in real-time:
```typescript
async function main(workbook: ExcelScript.Workbook) {
    const response = await fetch(
        "https://nsedata.YOUR_DOMAIN/api/v1/chain/NIFTY",
        { headers: { "X-API-Key": "your-api-key" } }
    );
    const data = await response.json();
    // Update Excel cells with option chain data
}
```

### Google Sheets (Apps Script â€” Auto-Refresh)
```javascript
function refreshOptionChain() {
  const url = "https://nsedata.YOUR_DOMAIN/api/v1/chain/NIFTY?expiry=2026-02-19";
  const options = { headers: { "X-API-Key": "your-api-key" } };
  const response = UrlFetchApp.fetch(url, options);
  const data = JSON.parse(response.getContentText()).data;
  
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("OptionChain");
  // Write chain data to sheet
  const rows = data.chain.map(s => [
    s.ce.oi, s.ce.change_oi, s.ce.volume, s.ce.ltp, s.ce.bid, s.ce.ask,
    s.strike,
    s.pe.bid, s.pe.ask, s.pe.ltp, s.pe.volume, s.pe.change_oi, s.pe.oi
  ]);
  sheet.getRange(2, 1, rows.length, 13).setValues(rows);
}

// Set up 1-minute trigger (minimum Google allows)
function createTrigger() {
  ScriptApp.newTrigger("refreshOptionChain")
    .timeBased()
    .everyMinutes(1)
    .create();
}
```

---

## 13. Environment Variables (`.env`)

```env
# ===== DATABASE =====
DB_USER=nseadmin
DB_PASSWORD=<generate-strong-password>
DB_NAME=nse_market_data

# ===== IIFL CAPITAL MARKETS API =====
IIFL_APP_KEY=YOUR_APP_KEY
IIFL_API_SECRET=YOUR_API_SECRET
IIFL_CLIENT_ID=YOUR_CLIENT_ID
IIFL_AUTH_CODE=YOUR_DAILY_AUTH_CODE
IIFL_REDIRECT_URL=http://localhost:3000/callback

# ===== SUBSCRIPTION (Phase 1) =====
SUBSCRIPTION_SYMBOL=NIFTY
SUBSCRIPTION_SEGMENT=NSEFO
STRIKE_RANGE=20

# ===== BACKFILL =====
ENABLE_BACKFILL=true
BACKFILL_DAYS=30

# ===== DATA API =====
DATA_API_KEY=<generate-api-key>
```

---

## 14. DNS Setup (Hostinger)

Add an **A record** on Hostinger for the new subdomain:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `nsedata` | `YOUR_VM_IP` | 300 |

This creates: `nsedata.YOUR_DOMAIN â†’ YOUR_VM_IP`

---

## 15. Azure VM NSG (Firewall) Rules

| Priority | Name | Port | Protocol | Source | Action | Notes |
|----------|------|------|----------|--------|--------|-------|
| Existing | SSH | 22 | TCP | Your IP | Allow | Already configured |
| Existing | HTTP | 80 | TCP | Any | Allow | Already configured |
| Existing | HTTPS | 443 | TCP | Any | Allow | Already configured |
| **NEW** | IIFL-WS | **8883** | TCP | Any | **Allow Outbound** | IIFL WebSocket (outbound only) |

> Note: Port 8088 (Data API) does NOT need to be opened â€” it's proxied through Nginx on 443.

---

## 16. Deployment Flow

```
Phase 1: DNS & SSL Setup
  â”œâ”€ 1.1  Add A record: nsedata.YOUR_DOMAIN â†’ YOUR_VM_IP (Hostinger)
  â”œâ”€ 1.2  Wait for DNS propagation (~5 min)
  â””â”€ 1.3  Issue SSL cert via certbot for nsedata.YOUR_DOMAIN

Phase 2: Deploy NSE Stack (on VM)
  â”œâ”€ 2.1  Create /home/azureuser/nse-market-data/ directory
  â”œâ”€ 2.2  Upload docker-compose.yml, Dockerfiles, source code
  â”œâ”€ 2.3  Create .env file with credentials
  â”œâ”€ 2.4  Verify existing containers are still running (docker ps)
  â”œâ”€ 2.5  docker compose up -d (in nse-market-data/)
  â”œâ”€ 2.6  Verify nse-timescaledb, nse-data-ingestor, nse-data-api are running
  â””â”€ 2.7  Verify existing containers are STILL running (safety check)

Phase 3: Nginx + SSL
  â”œâ”€ 3.1  Add nsedata.conf to nginx/conf.d/ (add to existing nginx volume)
  â”œâ”€ 3.2  Reload nginx container (docker exec nginx nginx -s reload)
  â””â”€ 3.3  Test https://nsedata.YOUR_DOMAIN/health

Phase 4: Data Verification
  â”œâ”€ 4.1  Check IIFL authentication works
  â”œâ”€ 4.2  Check WebSocket connection + subscription
  â”œâ”€ 4.3  Verify tick data flowing into TimescaleDB
  â”œâ”€ 4.4  Test API endpoints
  â””â”€ 4.5  Test SSE stream

Phase 5: Historical Backfill
  â”œâ”€ 5.1  Run backfill job (ENABLE_BACKFILL=true)
  â””â”€ 5.2  Verify historical_ohlcv table populated

Phase 6: Consumer Setup
  â”œâ”€ 6.1  Create Excel Power Query template
  â”œâ”€ 6.2  Create Google Sheets Apps Script
  â””â”€ 6.3  Test real-time refresh in both
```

---

## 17. Data Flow (Sequence)

```
[Pre-Market: 8:45 AM IST]
  â”‚
  â”œâ”€â–º Headless browser â†’ IIFL Login â†’ authCode
  â”œâ”€â–º POST /getusersession â†’ Bearer token
  â”œâ”€â–º Download NSEFO.json â†’ Filter NIFTY weekly options
  â”œâ”€â–º Calculate ATM Â± 20 strikes â†’ Build subscription list (~160 instruments)
  â”‚
[Market Open: 9:15 AM IST]
  â”‚
  â”œâ”€â–º WebSocket connects to bridge.iiflcapital.com:8883
  â”œâ”€â–º Subscribe: {"subscriptionList": ["nsefo/35005", "nsefo/35006", ...]}
  â”‚
  â”œâ”€â–º LOOP: Receive 188-byte binary packets
  â”‚     â”œâ”€â–º Parse binary â†’ {ltp, ohlc, depth, volume}
  â”‚     â”œâ”€â–º Push to in-memory broadcast queue (for SSE consumers, zero delay)
  â”‚     â”œâ”€â–º Buffer tick (100 ticks or 500ms)
  â”‚     â””â”€â–º Batch INSERT into tick_data hypertable
  â”‚
  â”œâ”€â–º PARALLEL: OI data via REST every 3 min â†’ INSERT into oi_data
  â”‚
  â”œâ”€â–º PARALLEL: Recalculate strike range every 5 min (as NIFTY moves)
  â”‚
[Consumer Request (anytime during market hours)]
  â”‚
  â”œâ”€â–º Excel/Sheets â†’ GET /api/v1/chain/NIFTY â†’ FastAPI â†’ TimescaleDB â†’ JSON
  â”œâ”€â–º OR: GET /api/v1/stream?symbols=NIFTY â†’ SSE â†’ real-time LTP updates
  â”‚
[Market Close: 3:30 PM IST]
  â”‚
  â”œâ”€â–º WebSocket disconnects gracefully
  â”œâ”€â–º Final buffer flush
  â””â”€â–º Compression jobs run on old tick data
```

---

## 18. Monitoring

| Metric | How | Alert |
|---|---|---|
| WebSocket connected | Health endpoint + heartbeat | Log + reconnect if dropped |
| Tick ingestion rate | Counter in health endpoint | Alert if 0 for >60s during market |
| DB write latency | Prometheus histogram (optional) | Warn if P95 > 500ms |
| Container health | Docker healthchecks | Auto-restart |
| Disk usage | `df -h` check in health endpoint | Alert at 80% |
| All existing containers | `docker ps` before/after deploy | Rollback if any stopped |

---

## 19. Rollback Plan

If anything goes wrong with existing containers:
```bash
# Stop ONLY NSE containers (existing are untouched)
cd /home/azureuser/nse-market-data
docker compose down

# Remove NSE nginx config
rm /home/azureuser/nginx/conf.d/nsedata.conf
docker exec nginx nginx -s reload

# Verify existing stack
cd /home/azureuser
docker compose ps   # All existing containers should show "Up"
```

---

## 20. File Structure (Final)

```
/home/azureuser/                           # ON AZURE VM
â”œâ”€â”€ docker-compose.yml                     # EXISTING (DO NOT MODIFY)
â”œâ”€â”€ nginx/conf.d/
â”‚   â”œâ”€â”€ default.conf                       # EXISTING
â”‚   â”œâ”€â”€ minio.conf                         # EXISTING
â”‚   â”œâ”€â”€ n8n.conf                           # EXISTING
â”‚   â””â”€â”€ nsedata.conf                       # âœ… NEW
â”œâ”€â”€ certbot/                               # EXISTING
â”‚
â””â”€â”€ nse-market-data/                       # âœ… NEW DIRECTORY
    â”œâ”€â”€ docker-compose.yml
    â”œâ”€â”€ .env
    â”œâ”€â”€ data-ingestor/
    â”‚   â”œâ”€â”€ Dockerfile
    â”‚   â”œâ”€â”€ requirements.txt
    â”‚   â””â”€â”€ src/
    â”‚       â”œâ”€â”€ main.py
    â”‚       â”œâ”€â”€ config.py
    â”‚       â”œâ”€â”€ auth/
    â”‚       â”œâ”€â”€ feed/
    â”‚       â”œâ”€â”€ db/
    â”‚       â”œâ”€â”€ instruments/
    â”‚       â”œâ”€â”€ backfill/
    â”‚       â”œâ”€â”€ broadcast/
    â”‚       â””â”€â”€ utils/
    â”œâ”€â”€ data-api/
    â”‚   â”œâ”€â”€ Dockerfile
    â”‚   â”œâ”€â”€ requirements.txt
    â”‚   â””â”€â”€ src/
    â”‚       â”œâ”€â”€ main.py
    â”‚       â”œâ”€â”€ config.py
    â”‚       â”œâ”€â”€ auth/
    â”‚       â”œâ”€â”€ routes/
    â”‚       â”œâ”€â”€ db/
    â”‚       â””â”€â”€ schemas/
    â”œâ”€â”€ scripts/
    â”‚   â””â”€â”€ init_db.sql
    â””â”€â”€ consumers/
        â”œâ”€â”€ excel/
        â””â”€â”€ google-sheets/

d:\Automation\Azure-Functions\              # LOCAL DEVELOPMENT
â”œâ”€â”€ .gemini/rules.md
â”œâ”€â”€ DESIGN.md
â”œâ”€â”€ config.json                             # IIFL credentials
â”œâ”€â”€ data-ingestor/                          # Mirror of VM code
â”œâ”€â”€ data-api/
â””â”€â”€ deploy.sh                               # SCP + SSH deploy script
```

---

## 21. Technology Stack Summary

| Component | Technology | Reason |
|---|---|---|
| **Database** | TimescaleDB (PG16) | Time-series optimized, separate from existing PG |
| **Data Ingestor** | Python 3.11 + asyncio | Async I/O for WebSocket + batch DB writes |
| **Binary Parser** | `struct` module | Parse IIFL 188-byte packets |
| **DB Driver** | asyncpg | Fastest async PG driver |
| **Data API** | FastAPI + SSE | REST + real-time streaming |
| **Automated Login** | Playwright headless | IIFL OAuth browser flow |
| **Reverse Proxy** | Nginx (existing) | Reuse existing container |
| **SSL** | Certbot (existing) | Reuse existing container |
| **Containerization** | Docker Compose | Separate stack, shared network |
| **Infrastructure** | Azure VM (YOUR_VM_NAME) | Reuse existing VM |

---

> **Next Steps:** Once you approve this design, I will begin deployment starting with Phase 1 (DNS setup) and Phase 2 (container deployment).

## 22. Binary Parser Bug â€” Root Cause & Resolution

> **Status:** âœ… RESOLVED â€” 2026-02-18  
> **Impact:** All price data (LTP, OHLC, bid/ask, depth) was incorrect for every instrument since initial deployment.  
> **DB Action:** All `tick_data`, `oi_data`, `historical_ohlcv`, `tick_data_suspect` tables truncated and fresh data ingestion started.

### Root cause

The binary parser in `data-ingestor/src/feed/__init__.py` (`parse_binary_packet()`) had the **entire 188-byte IIFL Market Feed packet field mapping shifted by 4 bytes**. It treated bytes 0-3 as an "instrument token" (discarded) and bytes 4-7 as LTP, but the official IIFL documentation specifies:

| Byte offset | Official field          | What old parser used it as |
|-------------|------------------------|---------------------------|
| 0-3         | **LTP** (Int32)        | "payload token" (discarded) |
| 4-7         | lastTradedQuantity     | LTP â† **wrong** |
| 8-11        | tradedVolume           | lastTradedQuantity |
| 12-15       | high                   | tradedVolume |
| 16-19       | low                    | open |
| 20-23       | open                   | high |
| 24-27       | close                  | low |
| 28-31       | averageTradedPrice     | close |
| 32-33       | reserved (UInt16)      | â€” |
| 34-37       | bestBidQuantity        | â€” |
| 38-41       | bestBidPrice           | â€” |
| 42-45       | bestAskQuantity        | â€” |
| 46-49       | bestAskPrice           | â€” |
| 50-53       | totalBidQuantity       | â€” |
| 54-57       | totalAskQuantity       | â€” |
| 58-61       | **priceDivisor**       | never read (hardcoded `100`) |
| 62-65       | lastTradedTime         | â€” |
| 66-185      | market depth (bids+asks) | wrong offsets + interleaved |

Additionally, the parser **hardcoded `divisor = 100.0`** instead of reading the `priceDivisor` field from bytes 58-61 of each packet.

### How it was diagnosed

1. Fetched official IIFL docs from https://developers.iiflcapital.com/apidocs/marketdatastream confirming the 188-byte packet structure.
2. Wrote `scripts/debug_mqtt_parsing.py` to capture raw MQTT binary packets and parse them both ways (old parser vs official layout).
3. Captured 3 live packets for instrument 64860 (NIFTY 25700 CE):
   - Old parser: LTP = 51.35, 29.90, 91.00 (wrong â€” these were actually lastTradedQuantity/100)
   - Official parser: LTP = 209.45, 209.95, 209.80 (correct â€” matches market)
4. Confirmed DB had been storing wrong values (LTP in hundreds of thousands) since deployment.

### Fix applied

**Files changed:**

1. `data-ingestor/src/feed/__init__.py` â€” Rewrote `parse_binary_packet()`:
   - Bytes 0-3 = LTP (Int32), not a token
   - Bytes 4-7 = lastTradedQuantity (UInt32)
   - Bytes 8-11 = tradedVolume (UInt32)
   - Bytes 12-15 = high, 16-19 = low, 20-23 = open, 24-27 = close
   - Bytes 28-31 = averageTradedPrice
   - Bytes 32-33 = reserved (UInt16)
   - Bytes 34-41 = bestBidQty + bestBidPrice
   - Bytes 42-49 = bestAskQty + bestAskPrice
   - Bytes 50-57 = totalBidQty + totalAskQty
   - Bytes 58-61 = **priceDivisor** (read dynamically, not hardcoded)
   - Bytes 62-65 = lastTradedTime
   - Bytes 66-125 = 5 bid depth levels (12 bytes each: qty + price + orders + padding)
   - Bytes 126-185 = 5 ask depth levels (12 bytes each)

2. `data-ingestor/src/feed/ws_client.py` â€” Removed bogus "token mismatch" warnings (the old "payload_token" was actually the LTP value, so it always differed from the topic instrument ID).

3. `scripts/compare_mqtt_api.py` and `scripts/compare_iifl_mqtt_instrument.py` â€” Fixed same parsing bug in diagnostic scripts.

### Correct packet structure reference

```
Offset  Size  Type    Field                        Notes
â”€â”€â”€â”€â”€â”€  â”€â”€â”€â”€  â”€â”€â”€â”€â”€â”€  â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€   â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
 0-3     4    Int32   ltp                          Divide by priceDivisor
 4-7     4    UInt32  lastTradedQuantity           Raw quantity
 8-11    4    UInt32  tradedVolume                 Raw volume
12-15    4    Int32   high                         Divide by priceDivisor
16-19    4    Int32   low                          Divide by priceDivisor
20-23    4    Int32   open                         Divide by priceDivisor
24-27    4    Int32   close                        Divide by priceDivisor
28-31    4    Int32   averageTradedPrice           Divide by priceDivisor
32-33    2    UInt16  reserved                     Ignore
34-37    4    UInt32  bestBidQuantity
38-41    4    Int32   bestBidPrice                 Divide by priceDivisor
42-45    4    UInt32  bestAskQuantity
46-49    4    Int32   bestAskPrice                 Divide by priceDivisor
50-53    4    UInt32  totalBidQuantity
54-57    4    UInt32  totalAskQuantity
58-61    4    Int32   priceDivisor                 Typically 100
62-65    4    Int32   lastTradedTime               Unix epoch
66-125  60    ...     bids[5]                      5 Ã— (qty:4 + price:4 + orders:2 + pad:2)
126-185 60    ...     asks[5]                      5 Ã— (qty:4 + price:4 + orders:2 + pad:2)
186-187  2    Int16   reserved                     Ignore
```

Source: https://developers.iiflcapital.com/apidocs/marketdatastream

### Verification

After deploying the fix and truncating all tick tables:

```
API avg LTP = 207.95  |  MQTT avg LTP = 207.95  |  diff = 0.00
âœ… API and MQTT LTPs are consistent
```

DB sample for instrument 64860:
```
time                          | ltp    | ltq   | open_price | high_price | low_price | close_price
2026-02-18 10:01:45.012448+00 | 207.95 | 24440 | 185.00     | 225.75     | 123.20    | 177.85
```

### Safeguards retained

The following safeguards from the investigation phase remain active:
- `LTP_SANITY_ENABLED=true` â€” drops ticks with >200% instantaneous LTP change
- `DROP_UNKNOWN_TICKS=true` â€” drops ticks for instruments not in the `instruments` table
- `GET /api/v1/ticks/{instrument_id}` endpoint â€” for debugging tick history
